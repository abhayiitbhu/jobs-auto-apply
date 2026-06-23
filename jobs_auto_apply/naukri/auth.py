from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import quote_plus, urljoin

from playwright.async_api import BrowserContext, Page

from ..cookies import inject_cookies as _inject

logger = logging.getLogger("job_apply")

NAUKRI_ORIGIN = "https://www.naukri.com"
NAUKRI_LOGIN = "https://www.naukri.com/nlogin/login"


async def inject_cookies(context: BrowserContext, cookies_path: Path) -> None:
    await _inject(
        context,
        cookies_path,
        default_domain=".naukri.com",
        required_names=None,
    )


async def verify_logged_in(page: Page, expected_name: str) -> bool:
    for url in (
        f"{NAUKRI_ORIGIN}/mnjuser/homepage",
        f"{NAUKRI_ORIGIN}/mnjuser/recommendedjobs",
        NAUKRI_ORIGIN,
    ):
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        if "nlogin" in page.url or "/login" in page.url.lower():
            continue
        login_link = page.get_by_role("link", name=re.compile(r"login|register", re.I))
        if await login_link.count() > 0 and await login_link.first.is_visible():
            continue
        body = (await page.locator("body").inner_text()).lower()
        if expected_name.lower() in body or "mnjuser" in page.url or "recommended" in body:
            return True
    return False
