from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .auth import NAUKRI_ORIGIN

logger = logging.getLogger("job_apply")

NAUKRI_PROFILE_URL = f"{NAUKRI_ORIGIN}/mnjuser/profile"

_FILE_INPUT_SELECTORS = (
    "#attachCV",
    'input[type="file"][id*="attach" i]',
    'input[type="file"][name*="attach" i]',
    'input[type="file"][accept*=".pdf"]',
    'input[type="file"][accept*="pdf"]',
    'input[type="file"]',
)

# Clicking these opens the OS file picker — only use with expect_file_chooser().
_UPLOAD_TRIGGER = re.compile(
    r"update\s*resume|upload\s*resume|attach\s*cv|replace\s*resume|upload\s*cv",
    re.I,
)

_SAVE_BUTTON = re.compile(
    r"^(save|submit|done)$|save\s*changes|update\s*profile",
    re.I,
)


async def _find_file_input(page: Page):
    for selector in _FILE_INPUT_SELECTORS:
        locator = page.locator(selector)
        if await locator.count() > 0:
            return locator.first
    return None


async def _attach_resume_to_input(page: Page, file_input, resume_path: Path) -> bool:
    try:
        await file_input.set_input_files(str(resume_path))
        return True
    except PlaywrightTimeout:
        logger.warning("Timed out attaching resume on Naukri profile")
    except Exception as exc:
        logger.warning("Failed to attach resume on Naukri profile: %s", exc)
    return False


async def _attach_via_file_chooser(page: Page, resume_path: Path) -> bool:
    """Click an upload control only inside expect_file_chooser so macOS Finder does not stay open."""
    for locator in (
        page.get_by_role("button", name=_UPLOAD_TRIGGER),
        page.get_by_role("link", name=_UPLOAD_TRIGGER),
        page.locator("span, a, button").filter(has_text=_UPLOAD_TRIGGER),
    ):
        if await locator.count() == 0:
            continue
        try:
            target = locator.first
            if not await target.is_visible():
                continue
            async with page.expect_file_chooser(timeout=8000) as chooser_info:
                await target.click()
            chooser = await chooser_info.value
            await chooser.set_files(str(resume_path))
            return True
        except PlaywrightTimeout:
            continue
        except Exception as exc:
            logger.debug("Naukri file chooser upload failed: %s", exc)
    return False


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

    attached = False
    file_input = await _find_file_input(page)
    if file_input is not None:
        # Hidden input: set files directly — no click, no OS file picker.
        attached = await _attach_resume_to_input(page, file_input, resume_path)

    if not attached:
        attached = await _attach_via_file_chooser(page, resume_path)

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
