from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..application_questions import discover_questions, fill_questions, resolve_question_answers
from ..apply_runner import run_apply_batch
from ..ats.apply import apply_on_company_site
from ..config import AppConfig
from ..cookies import is_external_career_url
from ..cover_letter import build_cover_letter, strip_markdown_emphasis
from ..page_load import goto_settled, prepare_interactive_page
from ..pending_questions import queue_unanswered
from ..resume_upload import upload_resume
from ..salary import is_job_salary_eligible, job_eligibility
from ..utils import JobListing, defer_job_for_run, job_key, record_abandoned_apply, save_applied_job
from .company import (
    extract_wellfound_company,
    extract_wellfound_company_about,
    looks_like_location_not_company,
)
from .guard import (
    WellfoundAccessRestrictedError,
    WellfoundApplicationLimitReached,
    is_access_restricted,
    is_application_limit_reached,
    resolve_post_submit,
)
from .modal import (
    click_apply,
    close_apply_modal,
    extract_wellfound_job_page,
    inspect_apply_modal,
    open_and_inspect_apply_modal,
)

logger = logging.getLogger("job_apply")

if TYPE_CHECKING:
    from .pipeline import CompanyGate


async def _raise_if_application_limit(page: Page) -> None:
    if await is_application_limit_reached(page):
        raise WellfoundApplicationLimitReached("Wellfound: maximum number of active applications reached")


def _unanswered_labels(questions: list[dict], answers: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for field in questions:
        label = str(field.get("label", "")).strip()
        if label and not str(answers.get(label, "")).strip():
            missing.append(label)
    return missing


def _record_location_blocked(config: AppConfig, job: JobListing) -> None:
    """Persist a location-blocked job so future runs do not reopen it."""
    record_abandoned_apply(
        config.applied_jobs_path,
        job_key(job.source, job.job_id),
        {
            "source": "wellfound",
            "title": job.title,
            "company": job.company,
            "url": job.url,
        },
        reason="location blocked",
    )


def _queue_missing(
    config: AppConfig,
    job: JobListing,
    questions: list[dict],
    answers: dict[str, str],
    *,
    reason: str = "need answers",
) -> list[str]:
    """Queue text questions we did not auto-fill and defer the job for re-apply."""
    missing = _unanswered_labels(questions, answers)
    if not missing:
        return []
    fields_by_label = {str(f.get("label", "")).strip(): f for f in questions if str(f.get("label", "")).strip()}
    queue_unanswered(
        config.base_dir,
        source="wellfound",
        job_title=job.title,
        company=job.company,
        job_url=job.url,
        labels=missing,
        job_id=job.job_id,
        fields_by_label=fields_by_label,
        config=config,
    )
    defer_job_for_run(config.applied_jobs_path, job, reason=reason)
    return missing


async def _resolve_and_fill_questions(
    page: Page,
    job: JobListing,
    config: AppConfig,
) -> bool:
    """Resolve modal questions; fill them or queue text ones for review.

    Returns True to proceed with submission, False to defer this job (questions
    were queued for manual verification and the job will be retried later).
    """
    if not config.application.interactive_questions:
        return True
    questions = await discover_questions(page)
    if not questions:
        return True
    answers = await resolve_question_answers(
        config,
        job,
        job.description,
        questions,
        interactive=False,
        defer_new=True,
        verify_free_text=True,
    )
    missing = _queue_missing(config, job, questions, answers)
    if missing:
        from ..run_issues import record_skip

        record_skip(
            source="wellfound",
            title=job.title,
            company=job.company,
            url=job.url,
            reason="need answers",
            questions=missing,
        )
        logger.info(
            "Queued %d question(s) for review; deferring %s @ %s",
            len(missing),
            job.title,
            job.company,
        )
        return False
    await fill_questions(page, answers)
    return True


async def _enrich_wellfound_job_on_page(page: Page, job: JobListing, config: AppConfig) -> None:
    min_lpa = config.application.min_inr_salary_lpa
    info = await extract_wellfound_job_page(page, min_inr_lpa=min_lpa)
    jd = info.jd
    modal_text = info.modal_text
    # Prefer the structured company name from __NEXT_DATA__; fall back to DOM scrape.
    company = info.company or await extract_wellfound_company(page, job.title, modal_text or jd)
    if company:
        job.company = company
    elif looks_like_location_not_company(job.company):
        job.company = ""
    job.description = jd
    if info.skills and not job.meta.get("skills"):
        job.meta["skills"] = info.skills
    if not job.meta.get("company_about"):
        about = info.company_about or await extract_wellfound_company_about(page, jd=jd)
        if about:
            job.meta["company_about"] = about
    if not job.meta.get("salary_display"):
        from ..salary import extract_salary_from_text

        snippet = info.salary_display or extract_salary_from_text(modal_text) or extract_salary_from_text(jd[:400])
        if snippet:
            job.meta["salary_display"] = snippet
    job.meta.update(
        job_eligibility(
            jd=jd,
            meta=job.meta,
            modal=modal_text,
            min_inr_lpa=min_lpa,
        )
    )


async def process_wellfound_job(
    page: Page,
    context: BrowserContext,
    job: JobListing,
    config: AppConfig,
    *,
    company_gate: CompanyGate | None = None,
    label: str = "",
) -> bool | None:
    """Open one job page, enrich, filter, and apply — single tab, single navigation."""
    prefix = f"{label} " if label else ""
    use_external = config.application.follow_external_from_wellfound and job.external_ats and not job.easy_apply
    if config.application.skip_external_ats and job.external_ats and not job.easy_apply and not use_external:
        logger.info("%sSkipping external ATS: %s", prefix, job.title)
        return None

    await goto_settled(page, job.url, timeout_ms=60_000)

    if await is_access_restricted(page):
        raise WellfoundAccessRestrictedError("Wellfound access restricted on job page")

    min_lpa = config.application.min_inr_salary_lpa

    await _enrich_wellfound_job_on_page(page, job, config)

    from ..role_filter import role_filter_kwargs, should_skip_role

    skip_role, role_reason = should_skip_role(
        job.title,
        jd=job.description,
        **role_filter_kwargs(config.profile),
    )
    if skip_role:
        logger.info("%sSkipping role: %s — %s", prefix, job.title, role_reason)
        return None

    if job.meta.get("eligible_to_apply") is False:
        if config.application.skip_location_blocked and job.meta.get("location_blocked"):
            _record_location_blocked(config, job)
        logger.info(
            "%sSkipping ineligible: %s — %s",
            prefix,
            job.title,
            job.meta.get("block_reason") or "blocked",
        )
        return None

    if config.application.skip_ineligible_salary and not is_job_salary_eligible(
        jd=job.description,
        meta=job.meta,
        min_inr_lpa=min_lpa,
    ):
        logger.info(
            "%sSkipping salary-ineligible: %s @ %s — %s",
            prefix,
            job.title,
            job.company,
            job.meta.get("salary_reason", "INR ≤ threshold"),
        )
        return None

    if company_gate is not None and not await company_gate.try_claim(job.company):
        logger.info("%sSkipping duplicate company: %s @ %s", prefix, job.title, job.company)
        return None

    logger.info("%s Applying: %s @ %s", prefix.rstrip(), job.title, job.company or "?")

    if use_external or (job.external_url and is_external_career_url(job.external_url)):
        external_url = job.external_url or await _resolve_external_url_from_wellfound(page, context)
        if external_url:
            if config.application.dry_run:
                logger.info("%s[DRY RUN] Would apply externally at %s", prefix, external_url)
                if company_gate is not None:
                    company_gate.release(job.company)
                return None
            success = await apply_on_company_site(page, job=job, config=config, url=external_url)
            if success:
                save_applied_job(
                    config.applied_jobs_path,
                    job_key("wellfound", job.job_id),
                    {
                        "source": "wellfound",
                        "title": job.title,
                        "company": job.company,
                        "external_url": external_url,
                    },
                )
            elif company_gate is not None:
                company_gate.release(job.company)
            return success

    if not await click_apply(page):
        await _raise_if_application_limit(page)
        logger.warning("%sCould not open apply modal for %s", prefix, job.url)
        if company_gate is not None:
            company_gate.release(job.company)
        return False

    await _raise_if_application_limit(page)

    info = await inspect_apply_modal(page, min_inr_lpa=min_lpa)
    info.jd = job.description or info.jd
    job.meta.update(
        job_eligibility(
            jd=job.description or info.jd,
            meta=job.meta,
            modal=info.modal_text,
            min_inr_lpa=min_lpa,
        )
    )
    elig = job.meta
    if config.application.skip_location_blocked and elig.get("location_blocked"):
        logger.info("%sSkipping location-blocked: %s @ %s", prefix, job.title, job.company)
        _record_location_blocked(config, job)
        await close_apply_modal(page)
        if company_gate is not None:
            company_gate.release(job.company)
        return None
    if config.application.skip_ineligible_salary and not elig.get("salary_eligible"):
        logger.info("%sSkipping salary-ineligible: %s — %s", prefix, job.title, elig.get("salary_reason"))
        await close_apply_modal(page)
        if company_gate is not None:
            company_gate.release(job.company)
        return None

    note = await build_cover_letter(
        config,
        job=job,
        page=page,
        jd=job.description,
        prefer_precomputed=False,
    )
    job.meta["cover_letter"] = note

    try:
        await _fill_cover_note(page, note)
    except PlaywrightTimeout:
        logger.debug("%sNo cover note field; continuing", prefix)

    await _upload_resume_in_modal(page, config)

    if not await _resolve_and_fill_questions(page, job, config):
        await close_apply_modal(page)
        if company_gate is not None:
            company_gate.release(job.company)
        return None

    if config.application.dry_run:
        logger.info("%s[DRY RUN] Would apply to %s @ %s", prefix, job.title, job.company)
        await close_apply_modal(page)
        if company_gate is not None:
            company_gate.release(job.company)
        return None

    submitted = await _submit_application_modal(page)
    if not submitted:
        logger.warning("%sCould not submit application for %s", prefix, job.url)
        await close_apply_modal(page)
        if company_gate is not None:
            company_gate.release(job.company)
        return False

    if not await _verify_wellfound_submit(page, job, prefix=prefix, company_gate=company_gate):
        return False

    save_applied_job(
        config.applied_jobs_path,
        job_key("wellfound", job.job_id),
        {"source": "wellfound", "title": job.title, "company": job.company, "url": job.url},
    )
    logger.info("%sApplied to %s @ %s", prefix, job.title, job.company)
    return True


async def ensure_resume_on_profile(page: Page, resume_path) -> None:
    await page.goto("https://wellfound.com/profile/edit", wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)

    if not await upload_resume(page, resume_path, save=True):
        logger.info("No resume upload field on profile; assuming resume already attached.")
        return
    logger.info("Resume uploaded to profile from %s", resume_path)


async def _fill_cover_note(page: Page, note: str) -> None:
    note = strip_markdown_emphasis(note)
    textarea = page.locator('textarea[placeholder*="note" i], textarea[placeholder*="message" i], textarea')
    await textarea.first.wait_for(state="visible", timeout=10000)
    await textarea.first.fill(note)


async def _upload_resume_in_modal(page: Page, config: AppConfig) -> None:
    """Attach a resume inside the apply modal only when a file field is shown."""
    if await page.locator('input[type="file"]').count() == 0:
        return
    await upload_resume(page, config.resume_path)


async def _submit_application_modal(page: Page) -> bool:
    await prepare_interactive_page(page, fast=False)
    for label in ("Send Application", "Send application", "Apply", "Submit"):
        btn = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
        if await btn.count() > 0:
            try:
                await btn.first.scroll_into_view_if_needed(timeout=5000)
                if await btn.first.is_visible():
                    await btn.first.click()
                    await page.wait_for_timeout(2500)
                    return True
            except PlaywrightTimeout:
                continue
    return False


async def _verify_wellfound_submit(
    page: Page,
    job: JobListing,
    *,
    prefix: str = "",
    company_gate: CompanyGate | None = None,
) -> bool:
    """Confirm submit succeeded; raise on application cap."""
    outcome = await resolve_post_submit(page)
    if outcome == "limit":
        await close_apply_modal(page)
        if company_gate is not None:
            company_gate.release(job.company)
        raise WellfoundApplicationLimitReached("Wellfound: maximum number of active applications reached")
    if outcome == "success":
        return True
    logger.warning("%sCould not confirm Wellfound submit for %s", prefix, job.url)
    await close_apply_modal(page)
    if company_gate is not None:
        company_gate.release(job.company)
    return False


async def _resolve_external_url_from_wellfound(page: Page, context: BrowserContext) -> str | None:
    link = page.get_by_role("link", name=re.compile(r"Apply", re.I))
    if await link.count() > 0:
        href = await link.first.get_attribute("href") or ""
        if href.startswith("http") and is_external_career_url(href):
            return href
        try:
            async with context.expect_page(timeout=10000) as pinfo:
                await link.first.click()
            new_page = await pinfo.value
            await new_page.wait_for_load_state("domcontentloaded")
            if is_external_career_url(new_page.url):
                return new_page.url
        except PlaywrightTimeout:
            pass
    return None


async def apply_to_job(
    page: Page,
    context: BrowserContext,
    job: JobListing,
    config: AppConfig,
) -> bool | None:
    """Return True if applied, None if intentionally skipped, False if failed."""
    use_external = config.application.follow_external_from_wellfound and job.external_ats and not job.easy_apply
    if config.application.skip_external_ats and job.external_ats and not job.easy_apply and not use_external:
        logger.info("Skipping external ATS job: %s @ %s", job.title, job.company)
        return None

    from ..role_filter import role_filter_kwargs, should_skip_role

    skip_role, role_reason = should_skip_role(
        job.title,
        jd=job.description,
        **role_filter_kwargs(config.profile),
    )
    if skip_role:
        logger.info("Skipping role: %s @ %s — %s", job.title, job.company, role_reason)
        return None

    if job.meta.get("eligible_to_apply") is False:
        if config.application.skip_location_blocked and job.meta.get("location_blocked"):
            _record_location_blocked(config, job)
        logger.info(
            "Skipping ineligible job: %s — %s",
            job.title,
            job.meta.get("block_reason") or "blocked",
        )
        return None

    if config.application.skip_ineligible_salary and not is_job_salary_eligible(
        jd=job.description,
        meta=job.meta,
        min_inr_lpa=config.application.min_inr_salary_lpa,
    ):
        logger.info(
            "Skipping salary-ineligible: %s @ %s — %s",
            job.title,
            job.company,
            job.meta.get("salary_reason", "INR ≤ threshold"),
        )
        return None

    await goto_settled(page, job.url)
    await page.wait_for_timeout(2000)
    min_lpa = config.application.min_inr_salary_lpa

    if use_external or (job.external_url and is_external_career_url(job.external_url)):
        external_url = job.external_url or await _resolve_external_url_from_wellfound(page, context)
        if external_url:
            if config.application.dry_run:
                logger.info("[DRY RUN] Would apply externally at %s", external_url)
                return None
            success = await apply_on_company_site(page, job=job, config=config, url=external_url)
            if success:
                save_applied_job(
                    config.applied_jobs_path,
                    job_key("wellfound", job.job_id),
                    {"source": "wellfound", "title": job.title, "company": job.company, "external_url": external_url},
                )
            return success

    info = await open_and_inspect_apply_modal(page, min_inr_lpa=min_lpa)
    await _raise_if_application_limit(page)
    if not info.opened:
        logger.warning("Could not open apply modal for %s", job.url)
        return False

    elig = info.eligibility
    job.meta.update(
        job_eligibility(
            jd=job.description or info.jd,
            meta=job.meta,
            modal=info.modal_text,
            min_inr_lpa=min_lpa,
        )
    )
    elig = job.meta
    if config.application.skip_location_blocked and elig.get("location_blocked"):
        logger.info("Skipping location-blocked: %s @ %s", job.title, job.company)
        _record_location_blocked(config, job)
        await close_apply_modal(page)
        return None
    if config.application.skip_ineligible_salary and not elig.get("salary_eligible"):
        logger.info("Skipping salary-ineligible: %s — %s", job.title, elig.get("salary_reason"))
        await close_apply_modal(page)
        return None

    job.description = info.jd or job.description
    note = await build_cover_letter(
        config,
        job=job,
        page=page,
        jd=job.description,
        prefer_precomputed=False,
    )
    job.meta["cover_letter"] = note

    try:
        await _fill_cover_note(page, note)
    except PlaywrightTimeout:
        logger.debug("No cover note field; continuing")

    await _upload_resume_in_modal(page, config)

    if not await _resolve_and_fill_questions(page, job, config):
        await close_apply_modal(page)
        return None

    if config.application.dry_run:
        logger.info("[DRY RUN] Would apply to %s @ %s", job.title, job.company)
        await close_apply_modal(page)
        return None

    submitted = await _submit_application_modal(page)
    if not submitted:
        logger.warning("Could not submit application for %s", job.url)
        await close_apply_modal(page)
        return False

    if not await _verify_wellfound_submit(page, job):
        return False

    save_applied_job(
        config.applied_jobs_path,
        job_key("wellfound", job.job_id),
        {"source": "wellfound", "title": job.title, "company": job.company, "url": job.url},
    )
    logger.info("Applied to %s @ %s", job.title, job.company)
    return True


async def apply_batch(
    page: Page,
    context: BrowserContext,
    jobs: list[JobListing],
    config: AppConfig,
) -> int:
    return await run_apply_batch(jobs, config, page, context, apply_to_job)
