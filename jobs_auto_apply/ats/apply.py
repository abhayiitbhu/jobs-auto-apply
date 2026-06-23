from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..cover_letter import build_cover_letter
from ..page_load import ensure_page_ready, goto_settled
from ..config import AppConfig, UserConfig
from ..cookies import split_name
from ..utils import JobListing
from .detector import detect_ats
from .workday import apply_workday

logger = logging.getLogger("job_apply")

APPLY_BUTTON_RE = re.compile(
    r"apply(\s+for\s+this\s+job)?|submit\s+application|i'?m\s+interested",
    re.I,
)
SUBMIT_RE = re.compile(r"submit(\s+application)?|send\s+application|apply\s+now", re.I)


async def _click_first_matching(page: Page, pattern: re.Pattern[str], *, role: str = "button") -> bool:
    locator = page.get_by_role(role, name=pattern)
    if await locator.count() > 0:
        for i in range(await locator.count()):
            item = locator.nth(i)
            if await item.is_visible():
                await item.click()
                return True
    link = page.get_by_role("link", name=pattern)
    if await link.count() > 0:
        for i in range(await link.count()):
            item = link.nth(i)
            if await item.is_visible():
                await item.click()
                return True
    return False


async def _fill_by_patterns(page: Page, patterns: list[str], value: str) -> bool:
    for pattern in patterns:
        field = page.locator(
            f'input[name*="{pattern}" i], input[id*="{pattern}" i], '
            f'input[placeholder*="{pattern}" i], input[aria-label*="{pattern}" i]'
        )
        if await field.count() > 0:
            await field.first.fill(value)
            return True
        label = page.get_by_label(re.compile(pattern, re.I))
        if await label.count() > 0:
            await label.first.fill(value)
            return True
    return False


async def _upload_resume(page: Page, resume_path: Path) -> bool:
    file_input = page.locator('input[type="file"]')
    if await file_input.count() == 0:
        return False
    for i in range(await file_input.count()):
        inp = file_input.nth(i)
        try:
            await inp.set_input_files(str(resume_path))
            return True
        except PlaywrightTimeout:
            continue
    return False


async def _fill_cover_letter(page: Page, note: str) -> None:
    for selector in (
        'textarea[name*="cover" i]',
        'textarea[id*="cover" i]',
        'textarea[placeholder*="cover" i]',
        'textarea[name*="message" i]',
        "textarea",
    ):
        area = page.locator(selector)
        if await area.count() > 0 and await area.first.is_visible():
            await area.first.fill(note)
            return


async def _fill_standard_fields(page: Page, user: UserConfig) -> None:
    first, last = split_name(user.name)
    await _fill_by_patterns(page, ["first_name", "firstname", "fname", "first"], first)
    await _fill_by_patterns(page, ["last_name", "lastname", "lname", "last"], last)
    await _fill_by_patterns(page, ["email", "e-mail"], user.email)
    await _fill_by_patterns(page, ["phone", "mobile", "tel"], user.phone)
    if user.linkedin:
        await _fill_by_patterns(page, ["linkedin", "linked_in", "linked in"], user.linkedin)


async def _open_apply_form(page: Page, ats: str) -> None:
    await page.wait_for_timeout(1500)
    if ats == "greenhouse":
        await _click_first_matching(page, re.compile(r"apply for this job", re.I))
        await page.wait_for_timeout(1000)
        return
    if ats == "lever":
        await _click_first_matching(page, re.compile(r"apply for this job", re.I))
        await page.wait_for_timeout(1000)
        return
    if ats == "ashby":
        await _click_first_matching(page, re.compile(r"apply", re.I))
        await page.wait_for_timeout(1000)
        return
    await _click_first_matching(page, APPLY_BUTTON_RE)


async def _submit_form(page: Page) -> bool:
    if await _click_first_matching(page, SUBMIT_RE):
        await page.wait_for_timeout(2500)
        return True
    submit = page.locator('button[type="submit"], input[type="submit"]')
    if await submit.count() > 0:
        await submit.first.click()
        await page.wait_for_timeout(2500)
        return True
    return False


async def apply_on_company_site(
    page: Page,
    *,
    job: JobListing,
    config: AppConfig,
    url: str | None = None,
) -> bool:
    target_url = url or job.external_url or job.url
    ats = detect_ats(target_url)

    logger.info("Opening company career page (%s): %s", ats, target_url)
    await goto_settled(page, target_url)
    await ensure_page_ready(page, for_form=True)
    note = await build_cover_letter(config, job=job, page=page)

    if ats == "workday":
        if config.application.dry_run:
            logger.info("[DRY RUN] Would run Workday apply flow at %s", target_url)
            return False
        return await apply_workday(page, config=config, job=job, note=note)

    await _open_apply_form(page, ats)

    if config.application.dry_run:
        logger.info("[DRY RUN] Would fill ATS form at %s", page.url)
        return False

    await _fill_standard_fields(page, config.user)
    await _upload_resume(page, config.resume_path)
    await _fill_cover_letter(page, note)

    # Some ATS use multi-step forms.
    for _ in range(3):
        if await _submit_form(page):
            body = (await page.locator("body").inner_text()).lower()
            if any(word in body for word in ("thank you", "application received", "submitted", "success")):
                return True
            if await page.locator('input[type="file"]').count() == 0:
                return True
        await _fill_standard_fields(page, config.user)
        await _upload_resume(page, config.resume_path)
        await _fill_cover_letter(page, note)

    logger.warning("Could not confirm submission on %s", page.url)
    return False
