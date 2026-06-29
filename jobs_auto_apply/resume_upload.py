from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

logger = logging.getLogger("job_apply")

# Site-agnostic file-input selectors, most specific first, ending with the bare input.
# Hidden inputs work fine with set_input_files (no OS picker is opened).
_FILE_INPUT_SELECTORS = (
    "#attachCV",
    'input[type="file"][id*="resume" i]',
    'input[type="file"][id*="cv" i]',
    'input[type="file"][id*="attach" i]',
    'input[type="file"][name*="resume" i]',
    'input[type="file"][name*="cv" i]',
    'input[type="file"][name*="attach" i]',
    'input[type="file"][accept*=".pdf"]',
    'input[type="file"][accept*="pdf"]',
    'input[type="file"][accept*=".doc"]',
    'input[type="file"]',
)

# Clicking these may open the OS file picker — only use inside expect_file_chooser().
_UPLOAD_TRIGGER = re.compile(
    r"(?:upload|attach|add|update|replace|change|choose|select|browse)\s*"
    r"(?:new\s*)?(?:a\s*)?(?:resume|cv|file|document)",
    re.I,
)

# Local-file option inside a source menu (Local / Drive / Dropbox / Paste).
_LOCAL_OPTION = re.compile(
    r"local|computer|my\s*device|this\s*device|upload\s*from\s*computer|" r"attach\s*a\s*file|browse|from\s*device",
    re.I,
)

# Cloud / paste options that require external auth — never click these.
_CLOUD_OPTION = re.compile(
    r"google\s*drive|drive|dropbox|onedrive|one\s*drive|box|paste|url|link",
    re.I,
)

_SAVE_BUTTON = re.compile(
    r"^(save|submit|done|update)$|save\s*changes|save\s*(&|and)?\s*next|update\s*profile",
    re.I,
)

# Confirms an upload landed, e.g. "uploaded", "uploaded on", a checkmark/success label.
_UPLOADED_TEXT = re.compile(r"uploaded|attached|upload\s*complete|success", re.I)


async def _find_file_input(scope):
    for selector in _FILE_INPUT_SELECTORS:
        locator = scope.locator(selector)
        if await locator.count() > 0:
            return locator.first
    return None


async def _attach_to_input(file_input, resume_path: Path) -> bool:
    """Set files on a (possibly hidden) input directly — no OS picker."""
    try:
        await file_input.set_input_files(str(resume_path))
        return True
    except PlaywrightTimeout:
        logger.warning("Timed out attaching resume to file input")
    except Exception as exc:
        logger.warning("Failed to attach resume to file input: %s", exc)
    return False


async def _attach_via_direct_input(scope, resume_path: Path) -> bool:
    file_input = await _find_file_input(scope)
    if file_input is None:
        return False
    return await _attach_to_input(file_input, resume_path)


async def _iter_trigger_candidates(scope):
    """Yield visible controls that look like an upload/attach trigger."""
    for locator in (
        scope.get_by_role("button", name=_UPLOAD_TRIGGER),
        scope.get_by_role("link", name=_UPLOAD_TRIGGER),
        scope.locator("label, span, a, button, div").filter(has_text=_UPLOAD_TRIGGER),
    ):
        count = await locator.count()
        for i in range(count):
            candidate = locator.nth(i)
            try:
                if await candidate.is_visible():
                    yield candidate
            except Exception:
                continue


async def _pick_local_option(scope):
    """Find the local-file option inside an opened source menu, skipping cloud options."""
    option = scope.get_by_text(_LOCAL_OPTION)
    count = await option.count()
    for i in range(count):
        candidate = option.nth(i)
        try:
            text = (await candidate.inner_text()).strip()
        except Exception:
            continue
        if _CLOUD_OPTION.search(text) and not _LOCAL_OPTION.search(text):
            continue
        try:
            if await candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


async def _attach_via_source_menu(page: Page, scope, resume_path: Path) -> bool:
    """Click an upload trigger that opens a source menu, choose the local option.

    Handles two sub-cases:
      * the local option wraps a hidden file input -> re-scan and set_input_files,
      * the local option opens the OS picker -> wrap the click in expect_file_chooser.
    Every click that could open the OS picker is wrapped so macOS Finder never stays open.
    """
    async for trigger in _iter_trigger_candidates(scope):
        # First, try the click as a file-chooser opener (covers the common single-step case).
        try:
            async with page.expect_file_chooser(timeout=4000) as chooser_info:
                await trigger.click()
            chooser = await chooser_info.value
            await chooser.set_files(str(resume_path))
            return True
        except PlaywrightTimeout:
            # No picker — the click likely opened a source menu instead. Fall through.
            pass
        except Exception as exc:
            logger.debug("Resume upload trigger click failed: %s", exc)
            continue

        # A source menu may now be open: an input could have appeared, or a local option.
        if await _attach_via_direct_input(scope, resume_path):
            return True

        local_option = await _pick_local_option(scope)
        if local_option is None:
            continue

        # Local option may wrap a hidden input, or open the OS picker on click.
        try:
            async with page.expect_file_chooser(timeout=4000) as chooser_info:
                await local_option.click()
            chooser = await chooser_info.value
            await chooser.set_files(str(resume_path))
            return True
        except PlaywrightTimeout:
            if await _attach_via_direct_input(scope, resume_path):
                return True
        except Exception as exc:
            logger.debug("Local source option click failed: %s", exc)

    return False


async def _verify_upload(page: Page, scope, resume_path: Path) -> bool:
    """Confirm the upload landed: filename text OR a success indicator appears."""
    name = resume_path.name
    stem = resume_path.stem
    for _ in range(4):
        try:
            if await scope.get_by_text(name, exact=False).count() > 0:
                return True
            if stem and stem != name and await scope.get_by_text(stem, exact=False).count() > 0:
                return True
            if await scope.get_by_text(_UPLOADED_TEXT).count() > 0:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    return False


async def _click_save(page: Page, scope) -> None:
    """Save/Update after attaching — never click a generic Upload control (re-opens picker)."""
    btn = scope.get_by_role("button", name=_SAVE_BUTTON)
    if await btn.count() == 0:
        return
    try:
        candidate = btn.first
        if await candidate.is_visible():
            await candidate.click()
            await page.wait_for_timeout(2500)
    except PlaywrightTimeout:
        pass
    except Exception as exc:
        logger.debug("Save after resume upload failed: %s", exc)


async def attach_resume(page: Page, resume_path: Path, *, scope=None) -> bool:
    """Attach a resume using the shared strategies (direct input + source menu).

    This performs only the *attach* step — no save, no verification — so callers
    (e.g. naukri/hirist) can keep their own save + date-based verification.
    Every picker-opening click is wrapped in ``expect_file_chooser``.
    """
    if not resume_path.exists():
        logger.warning("Resume not found at %s — skipping attach", resume_path)
        return False
    target = scope if scope is not None else page
    if await _attach_via_direct_input(target, resume_path):
        return True
    return await _attach_via_source_menu(page, target, resume_path)


async def upload_resume(page: Page, resume_path: Path, *, scope=None, save: bool = False) -> bool:
    """Upload a local resume into the first matching upload field within ``scope``.

    Strategy order (stops at the first that attaches AND verifies):
      1. Direct ``input[type="file"]`` (works for hidden inputs; no OS picker).
      2. Source-menu trigger -> choose the local-file option, skipping cloud sources,
         wrapping any picker-opening click in ``expect_file_chooser``.
      3. Verify via filename text or success UI (with short polling).

    When ``save`` is True, a Save/Update/Submit button is clicked after attaching
    (used by profile flows). Returns True only when attached AND verified.
    """
    if not resume_path.exists():
        logger.warning("Resume not found at %s — skipping upload", resume_path)
        return False

    target = scope if scope is not None else page

    attached = await _attach_via_direct_input(target, resume_path)
    if not attached:
        attached = await _attach_via_source_menu(page, target, resume_path)

    if not attached:
        logger.warning("No resume upload field found")
        return False

    # Give async uploads a moment to register before verifying.
    await page.wait_for_timeout(1500)

    if save:
        await _click_save(page, target)

    if await _verify_upload(page, target, resume_path):
        logger.info("Resume upload verified from %s", resume_path)
        return True

    logger.warning("Resume attached but could not verify upload from %s", resume_path)
    return False
