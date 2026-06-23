from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import BrowserContext, Page

from ..cookies import inject_cookies as _inject

INSTAHYRE_ORIGIN = "https://www.instahyre.com"
INSTAHYRE_OPPORTUNITIES = f"{INSTAHYRE_ORIGIN}/candidate/opportunities/"
INSTAHYRE_LOGIN = f"{INSTAHYRE_ORIGIN}/login/"


async def inject_cookies(context: BrowserContext, cookies_path: Path) -> None:
    await _inject(
        context,
        cookies_path,
        default_domain=".instahyre.com",
        required_names=None,
    )


async def verify_logged_in(page: Page, expected_name: str) -> bool:
    await page.goto(INSTAHYRE_OPPORTUNITIES, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    if "/login" in page.url:
        return False
    login_link = page.get_by_role("link", name=re.compile(r"log in|sign in", re.I))
    if await login_link.count() > 0 and await login_link.first.is_visible():
        return False
    body = (await page.locator("body").inner_text()).lower()
    return (
        expected_name.lower() in body
        or "opportunities" in page.url
        or "logout" in body
        or await page.get_by_role("button", name=re.compile(r"apply", re.I)).count() > 0
    )
