from __future__ import annotations

import re
from pathlib import Path

from playwright.async_api import BrowserContext, Page

from ..cookies import inject_cookies as _inject

HIRIST_ORIGIN = "https://www.hirist.tech"
HIRIST_LOGIN = f"{HIRIST_ORIGIN}/login"


async def inject_cookies(context: BrowserContext, cookies_path: Path) -> None:
    await _inject(
        context,
        cookies_path,
        default_domain=".hirist.tech",
        required_names=None,
    )


async def verify_logged_in(page: Page, expected_name: str) -> bool:
    for url in (f"{HIRIST_ORIGIN}/", f"{HIRIST_ORIGIN}/jobfeed", HIRIST_ORIGIN):
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        if "/login" in page.url.lower():
            continue
        login_btn = page.get_by_role("link", name=re.compile(r"login|sign in", re.I))
        if await login_btn.count() > 0 and await login_btn.first.is_visible():
            continue
        body = (await page.locator("body").inner_text()).lower()
        if expected_name.lower() in body or "logout" in body or "my jobs" in body:
            return True
        if await login_btn.count() == 0:
            return True
    return False
