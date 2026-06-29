from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..resume_upload import attach_resume
from .auth import NAUKRI_ORIGIN

logger = logging.getLogger("job_apply")

NAUKRI_PROFILE_URL = f"{NAUKRI_ORIGIN}/mnjuser/profile"

_SAVE_BUTTON = re.compile(
    r"^(save|submit|done)$|save\s*changes|update\s*profile",
    re.I,
)


async def _click_save_if_present(page: Page) -> None:
    """Save profile after attach — never click generic Upload (re-opens file explorer)."""
    btn = page.get_by_role("button", name=_SAVE_BUTTON)
    if await btn.count() == 0:
        return
    try:
        candidate = btn.first
        if await candidate.is_visible():
            await candidate.click()
            await page.wait_for_timeout(2500)
    except PlaywrightTimeout:
        pass


def _today_matches_update_text(text: str) -> bool:
    today = datetime.today()
    patterns = (
        today.strftime("%b %d, %Y"),
        f"{today.strftime('%b')} {today.day}, {today.strftime('%Y')}",
        today.strftime("%d %b %Y"),
        today.strftime("%d-%m-%Y"),
        today.strftime("%Y-%m-%d"),
    )
    normalized = text.strip().lower()
    return any(p.lower() in normalized for p in patterns)


async def ensure_resume_on_profile(page: Page, resume_path: Path) -> bool:
    """Upload the local resume to the Naukri profile. Returns True on success."""
    if not resume_path.exists():
        logger.warning("Resume not found at %s — skipping Naukri profile upload", resume_path)
        return False

    logger.info("Syncing resume to Naukri profile from %s", resume_path)
    await page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)

    if "nlogin" in page.url or "/login" in page.url.lower():
        logger.warning("Naukri profile page redirected to login — resume upload skipped")
        return False

    attached = await attach_resume(page, resume_path)

    if not attached:
        logger.warning("No resume upload field found on Naukri profile page")
        return False

    await page.wait_for_timeout(3000)
    await _click_save_if_present(page)

    update_locator = page.locator(
        '[class*="updateOn"], [class*="update-on"], [class*="lastUpdated"], [class*="last-updated"]'
    )
    if await update_locator.count() > 0:
        update_text = (await update_locator.first.inner_text()).strip()
        if _today_matches_update_text(update_text):
            logger.info("Naukri resume upload verified (last updated: %s)", update_text)
            return True
        if update_text:
            logger.info("Naukri resume attached; profile shows: %s", update_text)
            return True

    logger.info("Resume uploaded to Naukri profile from %s", resume_path)
    return True
