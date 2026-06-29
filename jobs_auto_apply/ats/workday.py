from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..config import AppConfig, WorkdayConfig
from ..cookies import split_name
from ..cover_letter import strip_markdown_emphasis
from ..resume_upload import upload_resume
from ..utils import JobListing

logger = logging.getLogger("job_apply")

SUCCESS_PHRASES = (
    "thank you",
    "application received",
    "application submitted",
    "successfully submitted",
    "your application has been",
)

FIRST_NAME_IDS = (
    "legalNameSection_firstName",
    "nameSection_firstName",
    "firstName",
)
LAST_NAME_IDS = (
    "legalNameSection_lastName",
    "nameSection_lastName",
    "lastName",
)
EMAIL_IDS = ("email", "emailAddress", "candidateEmailAddress")
PHONE_IDS = ("phoneNumber", "phoneNumber--nationalNumber", "mobilePhone")
NEXT_IDS = ("bottom-navigation-next-button", "pageFooterNextButton", "continueButton")
SUBMIT_IDS = ("submitButton", "pageFooterNextButton", "bottom-navigation-next-button")


async def _click_automation(page: Page, automation_id: str, *, timeout: int = 5000) -> bool:
    locator = page.locator(f'[data-automation-id="{automation_id}"]')
    if await locator.count() == 0:
        return False
    try:
        await locator.first.click(force=True, timeout=timeout)
        await page.wait_for_timeout(800)
        return True
    except PlaywrightTimeout:
        try:
            await locator.first.evaluate("el => el.click()")
            await page.wait_for_timeout(800)
            return True
        except Exception:
            return False


async def _fill_automation_input(page: Page, automation_id: str, value: str) -> bool:
    if not value:
        return False
    locator = page.locator(
        f'input[data-automation-id="{automation_id}"], textarea[data-automation-id="{automation_id}"]'
    )
    if await locator.count() == 0:
        return False
    field = locator.first
    if not await field.is_visible():
        return False
    await field.fill(value)
    return True


async def _fill_first_matching(page: Page, automation_ids: tuple[str, ...], value: str) -> bool:
    for automation_id in automation_ids:
        if await _fill_automation_input(page, automation_id, value):
            return True
    return False


async def _accept_cookies(page: Page) -> None:
    await _click_automation(page, "legalNoticeAcceptButton")


async def _already_applied(page: Page) -> bool:
    applied = page.locator('[data-automation-id="alreadyApplied"]')
    if await applied.count() > 0 and await applied.first.is_visible():
        logger.info("Workday: already applied to this job")
        return True
    body = (await page.locator("body").inner_text()).lower()
    return "you have already applied" in body


async def _open_apply_flow(page: Page) -> None:
    for automation_id in ("jobPostingApplyButton", "applyButton"):
        if await _click_automation(page, automation_id):
            await page.wait_for_timeout(1500)
            return

    apply_btn = page.get_by_role("button", name=re.compile(r"^apply$", re.I))
    if await apply_btn.count() > 0:
        await apply_btn.first.click(force=True)
        await page.wait_for_timeout(1500)
        return

    apply_link = page.get_by_role("link", name=re.compile(r"apply", re.I))
    if await apply_link.count() > 0:
        await apply_link.first.click(force=True)
        await page.wait_for_timeout(1500)


async def _choose_apply_manually(page: Page) -> None:
    if await _click_automation(page, "applyManually"):
        await page.wait_for_timeout(2000)
        return
    # Some tenants only show "Use my last application" vs manual — try autofill then continue.
    if await _click_automation(page, "autofillWithResume"):
        await page.wait_for_timeout(3000)


async def _handle_auth(page: Page, config: AppConfig) -> None:
    wd = config.workday
    if not wd.password:
        return

    sign_in_visible = await page.locator('input[data-automation-id="password"]').count() > 0
    create_visible = await page.locator('[data-automation-id="createAccountSubmitButton"]').count() > 0

    await _fill_automation_input(page, "email", config.user.email)

    if sign_in_visible:
        await _fill_automation_input(page, "password", wd.password)
        sign_in = page.locator(
            'div[role="button"][aria-label="Sign In"], '
            '[data-automation-id="signInSubmitButton"], '
            'button[data-automation-id="signInLink"]'
        )
        if await sign_in.count() > 0:
            await sign_in.first.click(force=True)
            await page.wait_for_timeout(3000)
        return

    if create_visible or await page.locator('input[data-automation-id="verifyPassword"]').count():
        await _fill_automation_input(page, "password", wd.password)
        await _fill_automation_input(page, "verifyPassword", wd.password)
        checkbox = page.locator(
            '[data-automation-id="createAccountCheckbox"], input[data-automation-id="createAccountCheckbox"]'
        )
        if await checkbox.count() > 0 and not await checkbox.first.is_checked():
            await checkbox.first.click(force=True)
        await _click_automation(page, "createAccountSubmitButton")
        await page.wait_for_timeout(3000)


async def _upload_resume(page: Page, resume_path: Path) -> bool:
    # Workday's hidden input is the fast path; fall back to the shared helper otherwise.
    wd_input = page.locator('input[data-automation-id="file-upload-input-ref"]')
    if await wd_input.count() > 0:
        try:
            await wd_input.first.set_input_files(str(resume_path))
            await page.wait_for_timeout(2500)
            return True
        except PlaywrightTimeout:
            pass
    return await upload_resume(page, resume_path)


async def _fill_address(page: Page, wd: WorkdayConfig) -> None:
    addr = wd.address
    await _fill_automation_input(page, "addressSection_addressLine1", addr.line1)
    await _fill_automation_input(page, "addressSection_city", addr.city)
    await _fill_automation_input(page, "addressSection_postalCode", addr.postal_code)

    if addr.country:
        await _select_dropdown_option(page, "addressSection_countryRegion", addr.country)
    if addr.state:
        await _select_dropdown_option(page, "addressSection_countryRegion", addr.state)


async def _select_dropdown_option(page: Page, container_id: str, option_label: str) -> bool:
    container = page.locator(f'[data-automation-id="{container_id}"]')
    if await container.count() == 0:
        container = page.locator(f'[data-automation-id="{container_id}"] button')
    if await container.count() == 0:
        return False

    try:
        await container.first.click(force=True)
        await page.wait_for_timeout(600)
        option = page.locator(
            f'[data-automation-id="promptOption"][data-automation-label="{option_label}"], '
            f'li[role="option"]:has-text("{option_label}"), '
            f'div[role="option"]:has-text("{option_label}")'
        )
        if await option.count() > 0:
            await option.first.click(force=True)
            await page.wait_for_timeout(400)
            return True
        # Typeahead fallback
        search = page.locator(f'[data-automation-id="{container_id}"] input, input[data-automation-id="searchBox"]')
        if await search.count() > 0:
            await search.first.fill(option_label)
            await page.wait_for_timeout(800)
            await page.keyboard.press("Enter")
            return True
    except PlaywrightTimeout:
        return False
    return False


async def _fill_how_did_you_hear(page: Page, source: str) -> None:
    for container_id in ("sourceSelector", "referralSection_source"):
        if await _select_dropdown_option(page, container_id, source):
            return
    # Nested source flow: Job Board → LinkedIn
    try:
        multi = page.locator('[data-automation-id="multiSelectContainer"]')
        if await multi.count() > 0:
            await multi.first.click(force=True)
            await page.wait_for_timeout(500)
            await page.locator('[data-automation-label="Job Board"], [data-automation-label="LinkedIn"]').first.click(
                force=True
            )
    except PlaywrightTimeout:
        pass


async def _fill_linkedin(page: Page, url: str) -> None:
    if not url:
        return
    for automation_id in ("linkedinQuestion", "urls--url", "socialNetworkAccounts--url"):
        if await _fill_automation_input(page, automation_id, url):
            return
    linkedin = page.locator('input[type="url"], input[aria-label*="LinkedIn" i]')
    if await linkedin.count() > 0:
        await linkedin.first.fill(url)


async def _fill_cover_letter(page: Page, note: str) -> None:
    note = strip_markdown_emphasis(note)
    for automation_id in (
        "coverLetter",
        "coverLetterText",
        "messageToHiringManager",
        "additionalInformation",
    ):
        if await _fill_automation_input(page, automation_id, note):
            return
    textarea = page.locator("textarea:visible")
    if await textarea.count() > 0:
        await textarea.first.fill(note)


async def _tick_agreement_checkboxes(page: Page) -> None:
    checkboxes = page.locator(
        'input[type="checkbox"]:not(:checked), '
        '[data-automation-id*="agreement" i], '
        '[data-automation-id*="consent" i], '
        '[data-automation-id*="checkbox" i]'
    )
    count = await checkboxes.count()
    for i in range(min(count, 6)):
        box = checkboxes.nth(i)
        try:
            if await box.is_visible() and not await box.is_checked():
                await box.click(force=True)
        except Exception:
            continue


async def _handle_voluntary_disclosures(page: Page, wd: WorkdayConfig) -> None:
    if not wd.skip_voluntary_disclosures:
        return
    decline_labels = (
        "Decline to Self Identify",
        "Decline To Self Identify",
        "I don't wish to answer",
        "Prefer not to answer",
        "Decline",
    )
    for label in decline_labels:
        option = page.get_by_text(label, exact=False)
        if await option.count() > 0:
            with contextlib.suppress(Exception):
                await option.first.click(force=True)


async def _fill_current_page(page: Page, config: AppConfig, note: str) -> None:
    user = config.user
    wd = config.workday
    first, last = split_name(user.name)

    await _fill_first_matching(page, FIRST_NAME_IDS, first)
    await _fill_first_matching(page, LAST_NAME_IDS, last)
    await _fill_first_matching(page, EMAIL_IDS, user.email)
    await _fill_first_matching(page, PHONE_IDS, user.phone)

    await _fill_address(page, wd)
    await _fill_how_did_you_hear(page, wd.how_did_you_hear)
    await _fill_linkedin(page, user.linkedin)
    await _upload_resume(page, config.resume_path)
    await _fill_cover_letter(page, note)
    await _handle_voluntary_disclosures(page, wd)
    await _tick_agreement_checkboxes(page)


async def _click_next(page: Page) -> bool:
    for automation_id in NEXT_IDS:
        btn = page.locator(f'[data-automation-id="{automation_id}"]')
        if await btn.count() == 0:
            continue
        for i in range(await btn.count()):
            candidate = btn.nth(i)
            if not await candidate.is_visible():
                continue
            disabled = await candidate.get_attribute("disabled")
            aria_disabled = await candidate.get_attribute("aria-disabled")
            if disabled is not None or aria_disabled == "true":
                continue
            text = (await candidate.inner_text()).lower()
            if "back" in text and "save" not in text:
                continue
            await candidate.click(force=True)
            await page.wait_for_timeout(2000)
            return True
    return False


async def _is_review_page(page: Page) -> bool:
    body = (await page.locator("body").inner_text()).lower()
    if "review" in body and "submit" in body:
        return True
    review = page.locator('[data-automation-id="reviewPage"]')
    return await review.count() > 0


async def _submit_application(page: Page) -> bool:
    for automation_id in SUBMIT_IDS:
        btn = page.locator(f'[data-automation-id="{automation_id}"]')
        if await btn.count() == 0:
            continue
        for i in range(await btn.count()):
            candidate = btn.nth(i)
            if not await candidate.is_visible():
                continue
            label = (await candidate.inner_text()).lower()
            if any(word in label for word in ("submit", "finish", "complete", "save and continue")):
                await candidate.click(force=True)
                await page.wait_for_timeout(3000)
                return True
    submit = page.get_by_role("button", name=re.compile(r"submit", re.I))
    if await submit.count() > 0:
        await submit.first.click(force=True)
        await page.wait_for_timeout(3000)
        return True
    return False


async def _confirm_success(page: Page) -> bool:
    await page.wait_for_timeout(2000)
    body = (await page.locator("body").inner_text()).lower()
    return any(phrase in body for phrase in SUCCESS_PHRASES)


async def apply_workday(
    page: Page,
    *,
    config: AppConfig,
    job: JobListing,
    note: str,
) -> bool:
    """Complete a Workday (myworkdayjobs.com) multi-step application."""
    title = job.title
    company = job.company
    wd = config.workday

    await page.wait_for_timeout(1500)
    await _accept_cookies(page)

    if await _already_applied(page):
        return False

    await _open_apply_flow(page)

    if config.application.dry_run:
        logger.info("[DRY RUN] Would start Workday apply flow at %s", page.url)
        return False

    await _choose_apply_manually(page)
    await _handle_auth(page, config)

    for step in range(wd.max_form_pages):
        logger.debug("Workday form step %d at %s", step + 1, page.url)
        await _fill_current_page(page, config, note)

        if await _is_review_page(page):
            await _tick_agreement_checkboxes(page)
            if await _submit_application(page) and await _confirm_success(page):
                logger.info("Workday application submitted for %s @ %s", title, company)
                return True
            break

        if not await _click_next(page):
            # Last page may use submit directly.
            if await _submit_application(page) and await _confirm_success(page):
                logger.info("Workday application submitted for %s @ %s", title, company)
                return True
            logger.warning("Workday: could not advance past step %d", step + 1)
            break

    logger.warning("Workday: could not confirm submission at %s", page.url)
    return False
