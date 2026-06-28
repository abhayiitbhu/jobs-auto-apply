from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .auth import HIRIST_ORIGIN

logger = logging.getLogger("job_apply")

# The resume upload widget lives on the "Personal Details" registration step;
# /myprofile is kept as a fallback for older account states.
HIRIST_PERSONAL_DETAILS_URL = f"{HIRIST_ORIGIN}/registration/addPersonalDetails"
HIRIST_PROFILE_URL = f"{HIRIST_ORIGIN}/myprofile"
_PROFILE_URLS = (HIRIST_PERSONAL_DETAILS_URL, HIRIST_PROFILE_URL)

_FILE_INPUT_SELECTORS = (
    "#attachCV",
    'input[type="file"][id*="resume" i]',
    'input[type="file"][id*="cv" i]',
    'input[type="file"][name*="resume" i]',
    'input[type="file"][name*="cv" i]',
    'input[type="file"][accept*=".pdf"]',
    'input[type="file"][accept*="pdf"]',
    'input[type="file"][accept*=".doc"]',
    'input[type="file"]',
)

# Clicking these opens the OS file picker — only use with expect_file_chooser().
_UPLOAD_TRIGGER = re.compile(
    r"upload\s*new\s*resume|update\s*resume|upload\s*resume|attach\s*cv|"
    r"attach\s*resume|replace\s*resume|upload\s*cv|change\s*resume",
    re.I,
)

_SAVE_BUTTON = re.compile(
    r"^(save|submit|done)$|save\s*(&|and)?\s*next|save\s*changes|update\s*profile",
    re.I,
)

# Confirms an upload completed, e.g. "abhay-jain.pdf (Uploaded On: 28-06-2026 12:48:38)".
_UPLOADED_TEXT = re.compile(r"uploaded\s*on", re.I)


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
        logger.warning("Timed out attaching resume on Hirist profile")
    except Exception as exc:
        logger.warning("Failed to attach resume on Hirist profile: %s", exc)
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
            logger.debug("Hirist file chooser upload failed: %s", exc)
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


async def _read_uploaded_status(page: Page) -> str | None:
    """Return the 'name.pdf (Uploaded On: ...)' confirmation text, if present."""
    locator = page.get_by_text(_UPLOADED_TEXT)
    if await locator.count() == 0:
        return None
    try:
        return (await locator.first.inner_text()).strip()
    except Exception:
        return None


async def _attach_on_current_page(page: Page, resume_path: Path) -> bool:
    if "/login" in page.url.lower():
        return False

    file_input = await _find_file_input(page)
    if file_input is not None:
        # The input is wrapped in a label; set files directly — no OS file picker.
        if await _attach_resume_to_input(page, file_input, resume_path):
            return True

    return await _attach_via_file_chooser(page, resume_path)


async def ensure_resume_on_profile(page: Page, resume_path: Path) -> bool:
    """Upload the local resume to the Hirist profile. Returns True on success."""
    if not resume_path.exists():
        logger.warning("Resume not found at %s — skipping Hirist profile upload", resume_path)
        return False

    logger.info("Syncing resume to Hirist profile from %s", resume_path)

    attached = False
    for url in _PROFILE_URLS:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        if "/login" in page.url.lower():
            logger.warning("Hirist page %s redirected to login — resume upload skipped", url)
            return False
        if await _attach_on_current_page(page, resume_path):
            attached = True
            break

    if not attached:
        logger.warning("No resume upload field found on Hirist profile page")
        return False

    # Wait for the "(Uploaded On: ...)" confirmation to refresh before saving.
    await page.wait_for_timeout(3000)
    status_text = await _read_uploaded_status(page)

    await _click_save_if_present(page)

    if status_text is None:
        status_text = await _read_uploaded_status(page)

    if status_text:
        if _today_matches_update_text(status_text):
            logger.info("Hirist resume upload verified (%s)", status_text)
        else:
            logger.info("Hirist resume attached; profile shows: %s", status_text)
        return True

    logger.info("Resume uploaded to Hirist profile from %s", resume_path)
    return True
