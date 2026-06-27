from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .chrome_profile import assert_chrome_profile_available, resolve_chrome_profile_dir
from .config import AppConfig
from .login import wait_for_manual_login
from .page_load import ensure_page_ready

logger = logging.getLogger("job_apply")

# Cookie-session platforms safe to run in parallel (each gets its own Chromium instance).
PARALLEL_COOKIE_PLATFORMS = frozenset({"naukri", "hirist", "instahyre"})


def _use_chrome_profile(config: AppConfig, platform: str) -> bool:
    if not config.browser.use_chrome_profile:
        return False
    return not (config.application.parallel_platforms and platform in PARALLEL_COOKIE_PLATFORMS)


VerifyFn = Callable[[Page, str], Awaitable[bool]]

STEALTH_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def _context_viewport_kwargs(config: AppConfig) -> dict:
    """Headed runs use the real window size so sticky Next/Submit footers stay visible."""
    if config.browser.headless:
        return {"viewport": {"width": 1440, "height": 1080}}
    return {"no_viewport": True}


# When many apply-workers run as background tabs in one window, Chromium throttles
# timers/animations and stops rendering occluded tabs — Naukri chatbot chips/radios
# then never finish animating in, so clicks silently no-op (text inputs still work).
# These flags keep every tab fully active regardless of foreground state.
_ANTI_THROTTLE_ARGS = [
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
]


def _browser_launch_kwargs(config: AppConfig) -> dict:
    kwargs: dict = {
        "headless": config.browser.headless,
        "slow_mo": config.browser.slow_mo_ms,
        "args": ["--disable-blink-features=AutomationControlled", *_ANTI_THROTTLE_ARGS],
    }
    if config.browser.chrome_channel:
        kwargs["channel"] = config.browser.chrome_channel
    if not config.browser.headless:
        kwargs["args"] = [*kwargs["args"], "--start-maximized"]
    return kwargs


def _session_path(config: AppConfig, platform: str) -> Path:
    return config.auth_sessions_dir / f"{platform}.json"


def _cookie_matches_host(cookie_domain: str, host: str) -> bool:
    """True when a stored cookie's domain applies to the active platform's host.

    Cookie domains may carry a leading dot (``.uplers.com``); a cookie set for a
    parent domain also applies to its subdomains, so match by host suffix.
    """
    cookie_domain = cookie_domain.lstrip(".").lower()
    host = host.lower()
    if not cookie_domain or not host:
        return False
    return host == cookie_domain or host.endswith("." + cookie_domain)


async def _create_ephemeral_context(
    playwright,
    config: AppConfig,
    storage_path: Path | None,
) -> tuple[Browser, BrowserContext]:
    browser = await playwright.chromium.launch(**_browser_launch_kwargs(config))
    context_kwargs: dict = {
        **_context_viewport_kwargs(config),
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "locale": "en-US",
    }
    if storage_path and storage_path.exists():
        context_kwargs["storage_state"] = str(storage_path)

    context = await browser.new_context(**context_kwargs)
    await context.add_init_script(STEALTH_INIT_SCRIPT)
    return browser, context


async def _launch_chrome_profile_context(playwright, config: AppConfig) -> BrowserContext:
    profile_dir = resolve_chrome_profile_dir(config)
    assert_chrome_profile_available(profile_dir)

    logger.info("Using Chrome profile: %s", profile_dir)
    print(
        f"\nUsing your Chrome profile: {profile_dir}\n"
        "Google / Wellfound / Uplers cookies from this profile will be reused.\n"
        "Quit Google Chrome completely (Cmd+Q) before running — do not open Chrome manually during the run.\n"
    )

    launch_kwargs: dict = {
        "user_data_dir": str(profile_dir),
        **_context_viewport_kwargs(config),
        "locale": "en-US",
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            *_ANTI_THROTTLE_ARGS,
        ],
        "ignore_default_args": ["--enable-automation"],
    }
    if not config.browser.headless:
        launch_kwargs["args"].append("--start-maximized")
    launch_kwargs.update(
        {k: v for k, v in _browser_launch_kwargs(config).items() if k in ("headless", "slow_mo", "channel")}
    )

    context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
    await context.add_init_script(STEALTH_INIT_SCRIPT)
    return context


async def _save_session(context: BrowserContext, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(path))
    logger.info("Saved browser session to %s", path)


async def _prepare_page(context: BrowserContext, origin: str) -> Page:
    """Navigate the primary tab to the target site (reuse tab — closing all tabs breaks persistent Chrome)."""
    pages = list(context.pages)
    page = pages[0] if pages else await context.new_page()
    for extra in pages[1:]:
        with suppress(Exception):
            await extra.close()

    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            await page.goto(origin, wait_until="domcontentloaded", timeout=90000)
            await ensure_page_ready(page, for_form=False)
            return page
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Navigation to %s failed (attempt %d/4): %s",
                origin,
                attempt + 1,
                exc,
            )
            if attempt < 3:
                await asyncio.sleep(1.5 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


async def _close_browser_context(browser: Browser | None, context: BrowserContext) -> None:
    if browser is not None:
        await browser.close()
    else:
        await context.close()
        # Persistent Chrome needs a moment to release the profile before relaunch.
        await asyncio.sleep(1.5)


async def _run_to_completion(coro: Awaitable):
    """Await *coro* fully even if this task is cancelled mid-way, returning its result.

    On ``serve --reload`` (or any shutdown) the in-flight apply cycle is
    cancelled while a Playwright driver round-trip is in progress (the driver
    handshake, a browser launch, or teardown). If that ``CancelledError``
    interrupts the round-trip, Python closes its end of the driver pipe while the
    Node driver is still mid-write, crashing it with an unhandled ``EPIPE``.
    Shielding each round-trip lets the driver always reach an idle state before
    the pipe closes, so it shuts down cleanly. If a cancellation was absorbed it
    is re-raised once the round-trip has finished.
    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Absorb cancellation aimed at us; keep waiting for the round-trip to finish.
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


@asynccontextmanager
async def browser_session(
    config: AppConfig,
    *,
    platform: str,
    origin: str,
    login_url: str,
    cookies_path: Path,
    inject_cookies_fn: Callable[[BrowserContext, Path], Awaitable[None]],
    verify_fn: VerifyFn,
    platform_label: str,
) -> AsyncIterator[tuple[Browser | None, BrowserContext, Page]]:
    storage_path = _session_path(config, platform)
    use_browser_auth = config.auth.method == "browser"
    use_chrome_profile = _use_chrome_profile(config, platform)

    # Start the driver under a shield and capture the Playwright object even if a
    # shutdown cancellation arrives mid-handshake, so the outer ``finally`` can
    # always stop the driver cleanly. An abandoned handshake (or launch) leaves
    # the Node driver mid-write; when Python then closes the pipe it crashes with
    # an unhandled EPIPE, so every bring-up round-trip is run to completion.
    _start_task = asyncio.ensure_future(async_playwright().start())
    _start_cancelled = False
    while not _start_task.done():
        try:
            await asyncio.shield(_start_task)
        except asyncio.CancelledError:
            _start_cancelled = True
    playwright = _start_task.result()
    try:
        if _start_cancelled:
            raise asyncio.CancelledError
        browser: Browser | None
        if use_chrome_profile:
            browser = None
            context = await _run_to_completion(_launch_chrome_profile_context(playwright, config))
        else:
            browser, context = await _run_to_completion(
                _create_ephemeral_context(
                    playwright,
                    config,
                    storage_path if use_browser_auth else None,
                )
            )

        if not use_chrome_profile and not use_browser_auth and cookies_path.exists():
            try:
                await inject_cookies_fn(context, cookies_path)
            except Exception as exc:
                logger.warning("Cookie injection failed: %s", exc)

        page = await _prepare_page(context, origin)
        logger.info("Opening %s and verifying session...", platform_label)

        logged_in = False
        for attempt in range(3):
            try:
                logged_in = await verify_fn(page, config.user.expected_display_name)
            except Exception as exc:
                logger.debug("Login verify attempt %d failed: %s", attempt + 1, exc)
                logged_in = False
            if logged_in:
                break
            if attempt < 2:
                await page.wait_for_timeout(3000)

        if not logged_in and use_chrome_profile and storage_path.exists():
            try:
                import json

                host = urlparse(origin).hostname or ""
                state = json.loads(storage_path.read_text(encoding="utf-8"))
                cookies = [c for c in state.get("cookies", []) if _cookie_matches_host(str(c.get("domain", "")), host)]
                if cookies:
                    await context.add_cookies(cookies)
                    logged_in = await verify_fn(page, config.user.expected_display_name)
                    if logged_in:
                        logger.info("Restored %s session from %s", platform_label, storage_path)
            except Exception as exc:
                logger.debug("Could not restore saved session: %s", exc)

        if not logged_in and not use_browser_auth and cookies_path.exists():
            await page.goto(origin, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            try:
                logged_in = await verify_fn(page, config.user.expected_display_name)
            except Exception:
                logged_in = False

        if not logged_in and use_browser_auth and not use_chrome_profile:
            if config.browser.headless:
                await _close_browser_context(browser, context)
                raise RuntimeError(
                    f"{platform_label} needs interactive Google login. "
                    "Set browser.headless: false, or enable browser.use_chrome_profile: true"
                )
            logged_in = await wait_for_manual_login(
                page,
                verify_fn=verify_fn,
                expected_name=config.user.expected_display_name,
                platform_label=platform_label,
                timeout_seconds=config.auth.login_timeout_seconds,
                login_url=login_url,
            )
            if logged_in:
                await _save_session(context, storage_path)

        if not logged_in and use_chrome_profile:
            if config.browser.headless:
                await _close_browser_context(browser, context)
                raise RuntimeError(
                    f"Not logged in to {platform_label} via Chrome profile. "
                    "Open Chrome, sign into Wellfound/Uplers with Google, quit Chrome, then retry."
                )
            print(
                f"\n{platform_label} session could not be verified in the automation browser.\n"
                "If you are already logged in via normal Chrome, this is usually bot-detection or a\n"
                "temporary Wellfound block — wait 30–60 min, keep apply_workers low, then retry.\n"
                "You can also sign in in the Playwright window below (same Google account).\n"
            )
            logged_in = await wait_for_manual_login(
                page,
                verify_fn=verify_fn,
                expected_name=config.user.expected_display_name,
                platform_label=platform_label,
                timeout_seconds=config.auth.login_timeout_seconds,
                login_url=origin,
                try_google_button=True,
            )
            if logged_in:
                await _save_session(context, storage_path)

        if not logged_in and not use_browser_auth:
            await _close_browser_context(browser, context)
            raise RuntimeError(
                f"Not logged in to {platform_label}. "
                "Enable browser.use_chrome_profile, run `python main.py login`, "
                "or use auth.method: cookies."
            )

        if not logged_in:
            await _close_browser_context(browser, context)
            raise RuntimeError(f"Could not log in to {platform_label}.")

        logger.info("%s session ready for '%s'", platform_label, config.user.expected_display_name)
        if use_chrome_profile and storage_path:
            try:
                await _save_session(context, storage_path)
            except Exception as exc:
                logger.debug("Could not snapshot session: %s", exc)

        async def _teardown() -> None:
            if use_browser_auth and not use_chrome_profile:
                try:
                    await _save_session(context, storage_path)
                except Exception as exc:
                    logger.warning("Could not save session: %s", exc)
            await _close_browser_context(browser, context)

        try:
            yield browser, context, page
        finally:
            await _run_to_completion(_teardown())
    finally:
        # Always stop the Playwright driver cleanly, even under cancellation,
        # so the Node driver process never crashes with an unhandled EPIPE.
        await _run_to_completion(playwright.stop())


@asynccontextmanager
async def wellfound_session(config: AppConfig) -> AsyncIterator[tuple[Browser | None, BrowserContext, Page]]:
    from .wellfound.auth import WELLFOUND_ORIGIN, inject_cookies, verify_logged_in

    async with browser_session(
        config,
        platform="wellfound",
        origin=WELLFOUND_ORIGIN,
        login_url=f"{WELLFOUND_ORIGIN}/login",
        cookies_path=config.cookies_path("wellfound"),
        inject_cookies_fn=inject_cookies,
        verify_fn=verify_logged_in,
        platform_label="Wellfound",
    ) as session:
        yield session


@asynccontextmanager
async def uplers_session(config: AppConfig) -> AsyncIterator[tuple[Browser | None, BrowserContext, Page]]:
    from .uplers.auth import UPLERS_ORIGIN, inject_cookies, verify_logged_in

    async with browser_session(
        config,
        platform="uplers",
        origin=UPLERS_ORIGIN,
        login_url=f"{UPLERS_ORIGIN}/talent/login",
        cookies_path=config.cookies_path("uplers"),
        inject_cookies_fn=inject_cookies,
        verify_fn=verify_logged_in,
        platform_label="Uplers",
    ) as session:
        yield session


@asynccontextmanager
async def naukri_session(config: AppConfig) -> AsyncIterator[tuple[Browser | None, BrowserContext, Page]]:
    from .naukri.auth import NAUKRI_LOGIN, NAUKRI_ORIGIN, inject_cookies, verify_logged_in

    async with browser_session(
        config,
        platform="naukri",
        origin=NAUKRI_ORIGIN,
        login_url=NAUKRI_LOGIN,
        cookies_path=config.cookies_path("naukri"),
        inject_cookies_fn=inject_cookies,
        verify_fn=verify_logged_in,
        platform_label="Naukri",
    ) as session:
        yield session


@asynccontextmanager
async def hirist_session(config: AppConfig) -> AsyncIterator[tuple[Browser | None, BrowserContext, Page]]:
    from .hirist.auth import HIRIST_LOGIN, HIRIST_ORIGIN, inject_cookies, verify_logged_in

    async with browser_session(
        config,
        platform="hirist",
        origin=HIRIST_ORIGIN,
        login_url=HIRIST_LOGIN,
        cookies_path=config.cookies_path("hirist"),
        inject_cookies_fn=inject_cookies,
        verify_fn=verify_logged_in,
        platform_label="Hirist",
    ) as session:
        yield session


@asynccontextmanager
async def instahyre_session(config: AppConfig) -> AsyncIterator[tuple[Browser | None, BrowserContext, Page]]:
    from .instahyre.auth import INSTAHYRE_LOGIN, INSTAHYRE_ORIGIN, inject_cookies, verify_logged_in

    async with browser_session(
        config,
        platform="instahyre",
        origin=INSTAHYRE_ORIGIN,
        login_url=INSTAHYRE_LOGIN,
        cookies_path=config.cookies_path("instahyre"),
        inject_cookies_fn=inject_cookies,
        verify_fn=verify_logged_in,
        platform_label="Instahyre",
    ) as session:
        yield session
