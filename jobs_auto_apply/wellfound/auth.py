from __future__ import annotations

import contextlib
import logging
import re

from playwright.async_api import Page

from ..cookies import inject_cookies as _inject
from .guard import is_access_restricted

logger = logging.getLogger("job_apply")

WELLFOUND_ORIGIN = "https://wellfound.com"

_SIGN_IN = re.compile(r"^(sign in|log in)$", re.I)


async def inject_cookies(context, cookies_path) -> None:
    await _inject(
        context,
        cookies_path,
        default_domain=".wellfound.com",
        required_names=["_wellfound"],
    )


async def _has_visible_sign_in(page: Page) -> bool:
    """Header sign-in only — footer links also say 'Sign in' when logged in."""
    for role in ("link", "button"):
        loc = page.get_by_role(role, name=_SIGN_IN)
        count = await loc.count()
        for i in range(min(count, 6)):
            el = loc.nth(i)
            if not await el.is_visible():
                continue
            box = await el.bounding_box()
            if box and box["y"] < 220:
                return True
    return False


async def _has_logged_in_nav(page: Page) -> bool:
    patterns = (
        'a[href*="/profile"]',
        'a[href*="/messages"]',
        'a[href*="/applications"]',
        'a[href*="/candidate"]',
        '[data-test="UserMenu"]',
        '[data-test="user-menu"]',
        'button[aria-label*="account" i]',
        'button[aria-label*="profile" i]',
    )
    for sel in patterns:
        loc = page.locator(sel)
        if await loc.count() > 0:
            try:
                if await loc.first.is_visible():
                    return True
            except Exception:
                continue
    return False


async def _has_session_cookie(page: Page) -> bool:
    names = {"_wellfound", "wellfound_session", "user_id", "remember_user_token"}
    for cookie in await page.context.cookies(WELLFOUND_ORIGIN):
        if cookie.get("name") in names and cookie.get("value"):
            return True
    return False


async def verify_logged_in(page: Page, expected_name: str) -> bool:
    await page.goto(f"{WELLFOUND_ORIGIN}/jobs", wait_until="domcontentloaded", timeout=90000)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(2000)

    if await is_access_restricted(page):
        logger.warning(
            "Wellfound shows 'Access is temporarily restricted' — wait 30-60 min, "
            "then retry with apply_workers: 2-3 and delay_seconds 2-5."
        )
        return False

    if "login" in page.url.lower():
        return False

    if await _has_logged_in_nav(page):
        return True

    if await _has_session_cookie(page) and not await _has_visible_sign_in(page):
        return True

    if await _has_visible_sign_in(page):
        return False

    try:
        body = (await page.locator("body").inner_text(timeout=8000)).lower()
    except Exception:
        body = ""

    name = (expected_name or "").strip().lower()
    if name and name in body:
        return True
    return any(token in body for token in ("log out", "logout", "my applications", "see more jobs"))
