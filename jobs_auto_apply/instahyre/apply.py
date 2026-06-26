from __future__ import annotations

import logging
import re

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..apply_runner import run_apply_batch
from ..config import AppConfig
from ..limits import apply_cap
from ..utils import JobListing, job_key, save_applied_job
from .auth import INSTAHYRE_OPPORTUNITIES
from .feeds import (
    InstahyreFeedSpec,
    _dismiss_apply_modal,
    _wait_for_opportunities,
    activate_feed,
    feeds_from_config,
)
from .search import (
    EMPLOYER_ROW,
    _scroll_load_more,
    click_view_at,
    employer_rows,
    job_id_from_card,
    parse_company_title,
    view_buttons,
)

logger = logging.getLogger("job_apply")

INSTAHYRE_DELAY_MS = 400  # default; overridden by application.platform_delays.instahyre_ms
APPLY_MODAL = ".application-modal.candidate-apply-modal, .modal.in, .modal.show, [role='dialog'], .apply-modal"


def _instahyre_delay_ms(config: AppConfig) -> int:
    return config.application.platform_delays.instahyre_ms


def _instahyre_advance_ms(config: AppConfig) -> int:
    return config.application.platform_delays.instahyre_advance_ms


async def _delay(page: Page, config: AppConfig) -> None:
    try:
        await page.wait_for_timeout(_instahyre_delay_ms(config))
    except PlaywrightError as exc:
        if "Target page, context or browser has been closed" in str(exc):
            raise
        logger.debug("Delay interrupted: %s", exc)


async def _read_job_panel(page: Page) -> tuple[str, str]:
    modal = page.locator(APPLY_MODAL)
    scope = modal.last if await modal.count() > 0 else page.locator(EMPLOYER_ROW).first
    if await modal.count() == 0 and await employer_rows(page).count() == 0:
        scope = page.locator("body")

    name_el = scope.locator(".employer-job-name .company-name, .employer-details .company-name, .modal-title, h1, h2")
    if await name_el.count() > 0:
        full = (await name_el.first.inner_text()).strip()
        if full:
            company, title = parse_company_title(full)
            return title, company

    text = await scope.inner_text()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    skip = {"view »", "view", "applied", "apply", "save", "hide", "share", "not interested"}
    filtered = [ln for ln in lines if ln.lower() not in skip]
    if not filtered:
        return "Unknown", ""
    company, title = parse_company_title(filtered[0])
    return title, company


_ADVANCE_NEXT_JOB_JS = """
() => {
  const wrap = document.querySelector(
    ".application-modal.candidate-apply-modal .application-modal-wrap"
  );
  if (!wrap) return { ok: false, reason: "no modal" };
  const scope = angular.element(wrap).scope();
  if (scope?.swipeOpp && scope?.opp) {
    scope.$apply(() => scope.swipeOpp(scope.opp, "next"));
    return { ok: true, via: "swipeOpp" };
  }
  return { ok: false, reason: "no swipeOpp" };
}
"""


async def _advance_to_next_job(page: Page, config: AppConfig) -> bool:
    """Move to next job in Instahyre's apply modal queue."""
    modal = page.locator(".application-modal.candidate-apply-modal")
    if await modal.count() == 0:
        return False

    before = await _read_job_panel(page)
    try:
        result = await page.evaluate(_ADVANCE_NEXT_JOB_JS)
        if isinstance(result, dict) and result.get("ok"):
            await page.wait_for_timeout(_instahyre_delay_ms(config))
            after = await _read_job_panel(page)
            return after != before
    except Exception:
        pass

    try:
        await page.keyboard.press("ArrowRight")
        await page.wait_for_timeout(_instahyre_advance_ms(config))
        after = await _read_job_panel(page)
        if after != before:
            return True
    except Exception:
        pass

    return False


async def _dismiss_modal(page: Page) -> None:
    await _dismiss_apply_modal(page)


async def _has_apply_button(page: Page) -> bool:
    scope = page.locator(APPLY_MODAL).last if await page.locator(APPLY_MODAL).count() > 0 else page
    for pattern in (r"^apply$", r"apply now", r"quick apply", r"^apply\s*»$"):
        btn = scope.get_by_role("button", name=re.compile(pattern, re.I))
        if await btn.count() > 0:
            try:
                if await btn.first.is_visible():
                    return True
            except Exception:
                continue
    return False


async def _click_apply(page: Page) -> bool:
    scope = page.locator(APPLY_MODAL).last if await page.locator(APPLY_MODAL).count() > 0 else page
    for pattern in (r"^apply$", r"apply now", r"quick apply", r"^apply\s*»$"):
        btn = scope.get_by_role("button", name=re.compile(pattern, re.I))
        if await btn.count() > 0:
            try:
                if await btn.first.is_visible():
                    await btn.first.click(timeout=5000)
                    return True
            except PlaywrightTimeout:
                continue
    fallback = scope.locator("button.btn-success, button.btn-primary").filter(has_text=re.compile(r"apply", re.I))
    if await fallback.count() > 0:
        try:
            await fallback.first.click(timeout=5000)
            return True
        except PlaywrightTimeout:
            pass
    return False


async def _try_apply_current(page: Page, config: AppConfig, feed_key: str) -> bool:
    """Click Apply on the open job modal when the button is visible."""
    title, company = await _read_job_panel(page)
    if config.application.dry_run:
        if await _has_apply_button(page):
            logger.info("[DRY RUN] Would apply on Instahyre: %s @ %s", title, company or "?")
        return False
    if not await _has_apply_button(page):
        return False
    if not await _click_apply(page):
        return False
    jid = job_id_from_card(title, company, feed_key)
    key = job_key("instahyre", jid)
    save_applied_job(
        config.applied_jobs_path,
        key,
        {"source": "instahyre", "title": title, "company": company, "url": feed_key},
    )
    logger.info("Applied on Instahyre: %s @ %s", title, company or "?")
    return True


async def _apply_chain_from_view(
    page: Page,
    config: AppConfig,
    feed_key: str,
    *,
    cap: int | None,
    already_applied: int,
) -> int:
    """Apply through Instahyre's modal queue — advance when Apply is missing."""
    applied = 0
    stuck = 0

    for _ in range(500):
        if cap is not None and already_applied + applied >= cap:
            break

        if config.application.dry_run:
            if await _has_apply_button(page):
                title, company = await _read_job_panel(page)
                logger.info("[DRY RUN] Would apply on Instahyre: %s @ %s", title, company or "?")
            elif not await _advance_to_next_job(page, config):
                stuck += 1
            else:
                stuck = 0
            if stuck >= 8:
                break
            continue

        if await _has_apply_button(page):
            if await _try_apply_current(page, config, feed_key):
                applied += 1
                stuck = 0
                if await _advance_to_next_job(page, config):
                    continue
            else:
                stuck += 1
        elif await _advance_to_next_job(page, config):
            stuck = 0
            continue
        else:
            stuck += 1

        if stuck >= 8:
            logger.info("Modal apply chain ended after %d applies on this feed segment", applied)
            break

        await _delay(page, config)

    return applied


async def apply_feed(page: Page, spec: InstahyreFeedSpec, config: AppConfig, applied_ids: set[str]) -> int:
    feed_key = await activate_feed(page, spec)

    applied = 0
    cap = apply_cap(config.application.max_jobs_per_run)
    load_rounds = 0
    stagnant_rounds = 0

    while load_rounds < 12 and stagnant_rounds < 4:
        # Scroll before checking view buttons — Instahyre hides buttons on applied rows
        # but may load more rows below after scrolling.
        before_rows = await employer_rows(page).count()
        await _scroll_load_more(page, rounds=3)
        after_rows = await employer_rows(page).count()
        view_count = await view_buttons(page).count()

        if view_count == 0:
            if after_rows > before_rows:
                stagnant_rounds = 0
                load_rounds += 1
                continue
            stagnant_rounds += 1
            logger.info(
                "No View buttons on feed %s (round %d, %d rows, stagnant %d/4)",
                spec.name,
                load_rounds + 1,
                after_rows,
                stagnant_rounds,
            )
            load_rounds += 1
            continue

        logger.info(
            "Found %d employer rows on feed %s (round %d, %d view buttons)",
            after_rows,
            spec.name,
            load_rounds + 1,
            view_count,
        )
        if not await click_view_at(page, 0):
            logger.warning("Could not click View on feed: %s", spec.name)
            stagnant_rounds += 1
            load_rounds += 1
            continue

        await page.wait_for_timeout(_instahyre_delay_ms(config))
        segment = await _apply_chain_from_view(page, config, feed_key, cap=cap, already_applied=applied)
        applied += segment

        if cap is not None and applied >= cap:
            break

        await _dismiss_modal(page)

        if segment > 0:
            stagnant_rounds = 0
            logger.info(
                "Applied %d on feed %s this round (%d total on feed)",
                segment,
                spec.name,
                applied,
            )
        else:
            stagnant_rounds += 1

        load_rounds += 1

    if stagnant_rounds >= 4:
        logger.info(
            "Finished feed %s after %d applies (no more actionable rows after scrolling)",
            spec.name,
            applied,
        )

    return applied


async def apply_from_feeds(
    page: Page,
    config: AppConfig,
    applied_ids: set[str],
    *,
    search_urls: list[str] | None = None,
    feed_dicts: list[dict] | None = None,
    default_job_functions: list[str] | None = None,
) -> int:
    total = 0
    for spec in feeds_from_config(
        search_urls=search_urls,
        feed_dicts=feed_dicts,
        default_job_functions=default_job_functions,
    ):
        total += await apply_feed(page, spec, config, applied_ids)
        cap = apply_cap(config.application.max_jobs_per_run)
        if cap is not None and total >= cap:
            break
    return total


async def apply_to_job(
    page: Page,
    _context: BrowserContext | None,
    job: JobListing,
    config: AppConfig,
) -> bool | None:
    feed_url = str(job.meta.get("feed_url", job.url or "")).strip()
    card_index = int(job.meta.get("card_index", 0))

    if not feed_url:
        logger.warning("No Instahyre feed URL for: %s", job.title)
        return False

    if feed_url not in page.url:
        await page.goto(f"{INSTAHYRE_OPPORTUNITIES}?matching=true", wait_until="domcontentloaded", timeout=90000)
        await _delay(page, config)
        await _wait_for_opportunities(page)

    if not await click_view_at(page, card_index):
        logger.warning("Could not click View for: %s", job.title)
        return False
    await _delay(page, config)

    if config.application.dry_run:
        logger.info("[DRY RUN] Would apply on Instahyre: %s", job.title)
        return None

    if not await _click_apply(page):
        logger.warning("No apply button for Instahyre job: %s", job.title)
        return False

    save_applied_job(
        config.applied_jobs_path,
        job_key("instahyre", job.job_id),
        {"source": "instahyre", "title": job.title, "company": job.company, "url": feed_url},
    )
    logger.info("Applied on Instahyre: %s @ %s", job.title, job.company or "?")
    return True


async def apply_batch(
    page: Page,
    context: BrowserContext | None,
    jobs: list[JobListing],
    config: AppConfig,
) -> int:
    # Instahyre applies sequentially (one job at a time); parallel tabs caused
    # feed/modal races, so it intentionally ignores instahyre_apply_workers.
    return await run_apply_batch(
        jobs,
        config,
        page,
        context,
        apply_to_job,
        workers=1,
    )
