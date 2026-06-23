from __future__ import annotations

import logging
import re
import time

from playwright.async_api import Page

logger = logging.getLogger("job_apply")


async def _click_google_sign_in(page: Page) -> None:
    """Try to open the Google OAuth flow; user completes passkey/2FA in the browser."""
    patterns = (
        re.compile(r"continue with google", re.I),
        re.compile(r"sign in with google", re.I),
        re.compile(r"log in with google", re.I),
        re.compile(r"google", re.I),
    )
    for pattern in patterns:
        for role in ("button", "link"):
            locator = page.get_by_role(role, name=pattern)
            if await locator.count() > 0:
                try:
                    await locator.first.click(timeout=5000)
                    await page.wait_for_timeout(1500)
                    logger.info("Clicked Google sign-in button")
                    return
                except Exception:
                    continue
        locator = page.locator("a, button").filter(has_text=pattern)
        if await locator.count() > 0:
            try:
                await locator.first.click(timeout=5000)
                await page.wait_for_timeout(1500)
                logger.info("Clicked Google sign-in control")
                return
            except Exception:
                continue


async def wait_for_manual_login(
    page: Page,
    *,
    verify_fn,
    expected_name: str,
    platform_label: str,
    timeout_seconds: int,
    login_url: str,
    try_google_button: bool = True,
) -> bool:
    """
    Open the login page and wait for the user to finish Google OAuth (passkey, 2FA, etc.)
  in the visible browser window.
    """
    await page.goto(login_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    if try_google_button:
        await _click_google_sign_in(page)

    print(
        f"\n{'=' * 60}\n"
        f"  {platform_label}: log in with your Gmail account in the browser window.\n"
        f"  Use your passkey, Google prompt, or 2FA as you normally would.\n"
        f"  Waiting up to {timeout_seconds // 60} minutes...\n"
        f"{'=' * 60}\n"
    )

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if await verify_fn(page, expected_name):
                print(f"\n[OK] {platform_label} login successful.\n")
                return True
        except Exception:
            pass
        await page.wait_for_timeout(2000)

    print(f"\n[FAIL] {platform_label} login timed out after {timeout_seconds}s.\n")
    return False
