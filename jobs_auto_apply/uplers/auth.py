from __future__ import annotations

from pathlib import Path

from playwright.async_api import BrowserContext, Page

from ..cookies import inject_cookies as _inject

UPLERS_ORIGIN = "https://platform.uplers.com"

UPLERS_JOBS_URLS = (
    "https://platform.uplers.com/talent/opportunities",
    "https://platform.uplers.com/talent/jobs",
    "https://platform.uplers.com/talent/dashboard",
)


async def inject_cookies(context: BrowserContext, cookies_path: Path) -> None:
    await _inject(
        context,
        cookies_path,
        default_domain=".uplers.com",
        required_names=None,
    )


async def verify_logged_in(page: Page, expected_name: str) -> bool:
    for url in UPLERS_JOBS_URLS:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        if "login" in page.url.lower() or "joinus" in page.url.lower():
            continue
        body = (await page.locator("body").inner_text()).lower()
        if expected_name.lower() in body or "opportunit" in body or "my profile" in body:
            return True
        sign_in = page.get_by_role("link", name="Login")
        login_btn = page.get_by_role("button", name="Login")
        if await sign_in.count() == 0 and await login_btn.count() == 0:
            return True
    return False
