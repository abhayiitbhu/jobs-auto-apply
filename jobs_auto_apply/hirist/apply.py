from __future__ import annotations

import logging
import random
import re

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from .questions import (
    click_hirist_advance,
    discover_hirist_questions,
    fill_hirist_questions,
    default_checkbox_answer,
)
from ..page_load import goto_settled, prepare_interactive_page, reveal_footer_actions
from ..application_questions import resolve_question_answers
from ..apply_runner import run_apply_batch
from ..config import AppConfig
from ..pending_questions import queue_unanswered
from ..utils import JobListing, job_key, save_applied_job

logger = logging.getLogger("job_apply")


async def _page_jd(page: Page) -> str:
    for sel in (
        '[class*="job-description"]',
        '[class*="JobDescription"]',
        '[class*="description"]',
        "article",
        "main",
    ):
        loc = page.locator(sel)
        if await loc.count() > 0:
            text = (await loc.first.inner_text()).strip()
            if len(text) > 120:
                return text[:12000]
    return (await page.locator("body").inner_text())[:12000]


async def _already_applied(page: Page) -> bool:
    body = (await page.locator("body").inner_text()).lower()
    if "you have already applied" in body or "already applied" in body:
        return True
    applied_btn = page.locator("button, a").filter(has_text=re.compile(r"^applied$", re.I))
    if await applied_btn.count() > 0:
        try:
            if await applied_btn.first.is_visible():
                return True
        except Exception:
            pass
    return False


async def _click_apply(page: Page) -> bool:
    for pattern in (r"^apply now$", r"^quick apply$", r"^apply$"):
        btn = page.get_by_role("button", name=re.compile(pattern, re.I))
        if await btn.count() > 0:
            try:
                await btn.first.click(timeout=5000)
                return True
            except PlaywrightTimeout:
                continue
    link = page.get_by_role("link", name=re.compile(r"^apply$", re.I))
    if await link.count() > 0:
        try:
            await link.first.click(timeout=5000)
            return True
        except PlaywrightTimeout:
            pass
    fallback = page.locator("button, a").filter(has_text=re.compile(r"apply now|quick apply", re.I))
    if await fallback.count() > 0:
        try:
            await fallback.first.click(timeout=5000)
            return True
        except PlaywrightTimeout:
            pass
    return False


async def _application_success(page: Page) -> bool:
    url = page.url.lower()
    body = (await page.locator("body").inner_text()).lower()
    if any(
        phrase in body
        for phrase in (
            "successfully applied",
            "application submitted",
            "you have applied",
            "applied successfully",
            "your application has been",
            "thank you for applying",
            "application sent",
            "successfully submitted",
            "has been submitted",
        )
    ):
        return True
    if "already applied" in body:
        return True
    applied_btn = page.locator("button, a").filter(has_text=re.compile(r"^applied$", re.I))
    if await applied_btn.count() > 0:
        try:
            if await applied_btn.first.is_visible():
                return True
        except Exception:
            pass
    # Submitted forms usually leave /screening and show Applied on the job page.
    if "/screening" not in url and "mandatory question" not in body:
        if await _already_applied(page):
            return True
    return False


async def _click_advance_button(page: Page) -> str | None:
    """Click Hirist Next / Submit / Confirm."""
    await prepare_interactive_page(page, fast=False)

    for attempt in range(2):
        clicked = await click_hirist_advance(page)
        if clicked:
            logger.info("Hirist: clicked %s", clicked)
            return clicked
        if attempt == 0:
            await reveal_footer_actions(page, for_form=True)
        await page.wait_for_timeout(400)

    patterns: tuple[tuple[str, str], ...] = (
        (r"submit application", "submit"),
        (r"^submit$", "submit"),
        (r"^next$", "next"),
        (r"^confirm$", "confirm"),
        (r"^finish$", "finish"),
        (r"^done$", "done"),
        (r"^continue$", "continue"),
        (r"^proceed$", "proceed"),
    )
    for pattern, label in patterns:
        role_btn = page.get_by_role("button", name=re.compile(pattern, re.I))
        locators = [role_btn]
        locators.append(
            page.locator("button, input[type=submit], a[role=button], a.btn, a[class*='btn']").filter(
                has_text=re.compile(pattern, re.I)
            )
        )
        for loc in locators:
            count = await loc.count()
            for i in range(count - 1, -1, -1):
                candidate = loc.nth(i)
                try:
                    if not await candidate.is_visible():
                        continue
                    href = await candidate.get_attribute("href") or ""
                    if href and re.search(r"/j/|/job/", href):
                        continue
                    await candidate.scroll_into_view_if_needed()
                    await candidate.click(timeout=5000)
                    logger.info("Hirist: clicked %s", label)
                    await page.wait_for_timeout(2000)
                    return label
                except PlaywrightTimeout:
                    continue
    return None


def _unanswered_labels(questions: list, answers: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for field in questions:
        label = field.get("label", "").strip()
        if not label:
            continue
        if answers.get(label, "").strip():
            continue
        if default_checkbox_answer(label, str(field.get("kind", "text"))):
            continue
        missing.append(label)
    return missing


def _queue_missing(
    config: AppConfig,
    job: JobListing,
    questions: list,
    answers: dict[str, str],
) -> list[str]:
    missing = _unanswered_labels(questions, answers)
    if missing:
        fields_by_label = {
            str(f.get("label", "")).strip(): f
            for f in questions
            if str(f.get("label", "")).strip()
        }
        queue_unanswered(
            config.base_dir,
            source="hirist",
            job_title=job.title,
            company=job.company,
            job_url=job.url,
            labels=missing,
            fields_by_label=fields_by_label,
        )
    return missing


async def _wait_for_screening_form(page: Page) -> None:
    try:
        await page.wait_for_selector(
            "text=/Mandatory Question|tell the recruiter more about yourself/i",
            timeout=12000,
        )
    except PlaywrightTimeout:
        pass
    await page.wait_for_timeout(1000)


async def apply_to_job(
    page: Page,
    _context: BrowserContext | None,
    job: JobListing,
    config: AppConfig,
) -> bool | None:
    await goto_settled(page, job.url)

    if await _already_applied(page):
        logger.info("Already applied on Hirist: %s", job.title)
        return None

    jd = await _page_jd(page)

    if config.application.dry_run:
        logger.info("[DRY RUN] Would apply on Hirist: %s", job.title)
        return None

    if not await _click_apply(page):
        logger.warning("No apply button on Hirist: %s", job.url)
        return False

    await page.wait_for_timeout(2000)
    await _wait_for_screening_form(page)

    max_steps = 6
    for step in range(max_steps):
        questions = await discover_hirist_questions(page)
        answers: dict[str, str] = {}
        if questions:
            logger.info(
                "Hirist: %d question(s) for %s (step %d)",
                len(questions),
                job.title,
                step + 1,
            )
            answers = await resolve_question_answers(
                config,
                job,
                jd,
                questions,
                interactive=False,
                confirm_new=False,
                defer_new=True,
            )
            missing = _queue_missing(config, job, questions, answers)
            if missing:
                from ..run_issues import record_skip

                record_skip(
                    source="hirist",
                    title=job.title,
                    company=job.company,
                    url=job.url,
                    reason="need answers",
                    questions=missing,
                )
                logger.warning(
                    "Skipped apply (need answers): %s — %d question(s) queued",
                    job.title,
                    len(missing),
                )
                return None
            unfilled = await fill_hirist_questions(page, questions, answers)
            if unfilled:
                _queue_missing(
                    config, job, [q for q in questions if q["label"] in unfilled], answers
                )
                logger.warning(
                    "Skipped apply (could not fill %d field(s)): %s",
                    len(unfilled),
                    job.title,
                )
                return None
            await page.wait_for_timeout(200)
            await prepare_interactive_page(page, fast=False)

        if await _application_success(page):
            break

        action = await _click_advance_button(page)
        if not action:
            if questions:
                logger.warning(
                    "Hirist: could not click Next/Submit after filling %d question(s) for %s",
                    len(questions),
                    job.url,
                )
            elif step == 0:
                logger.warning("No Hirist Next/Submit button for %s", job.url)
            break

        if await _application_success(page):
            break

        await page.wait_for_timeout(1500)

    if not await _application_success(page):
        logger.warning("Could not confirm Hirist submit for %s", job.url)
        return False

    save_applied_job(
        config.applied_jobs_path,
        job_key("hirist", job.job_id),
        {"source": "hirist", "title": job.title, "company": job.company, "url": job.url},
    )
    logger.info("Applied on Hirist: %s @ %s", job.title, job.company or "?")
    return True


async def apply_batch(
    page: Page,
    context: BrowserContext | None,
    jobs: list[JobListing],
    config: AppConfig,
) -> int:
    applied = 0
    for job in jobs:
        try:
            result = await apply_to_job(page, context, job, config)
            if result is True:
                applied += 1
        except Exception:
            logger.exception("Hirist apply failed: %s", job.url)
        lo = max(0, config.application.delay_seconds_min)
        hi = max(lo, config.application.delay_seconds_max)
        if hi > 0:
            await page.wait_for_timeout(random.randint(lo, hi) * 1000)
    return applied
