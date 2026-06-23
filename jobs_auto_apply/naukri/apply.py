from __future__ import annotations

import logging
import random
import re

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..application_questions import resolve_question_answers
from ..page_load import goto_settled, prepare_interactive_page
from ..config import AppConfig
from ..cover_letter import build_cover_letter
from ..pending_questions import queue_unanswered
from ..utils import JobListing, job_key, save_applied_job
from .questions import (
    chatbot_is_open,
    discover_naukri_chatbot_questions,
    fill_naukri_chatbot_question,
    wait_for_chatbot,
)

logger = logging.getLogger("job_apply")

_AURUS_JD_SELECTOR = '#jobs-desc, [style*="view-transition-name: job-title"]'
_QUICK_APPLY_RE = re.compile(r"quick apply", re.I)
_MAX_CHATBOT_STEPS = 15


def _job_detail_url(url: str) -> str:
    base = url.split("?")[0]
    if "job-listings" not in base:
        return url
    return f"{base}?src=directSearch"


async def _wait_for_job_detail(page: Page) -> None:
    try:
        await page.wait_for_selector(_AURUS_JD_SELECTOR, timeout=12000)
    except PlaywrightTimeout:
        pass
    try:
        await page.wait_for_selector("#jobs-desc button", timeout=8000)
    except PlaywrightTimeout:
        pass
    await _wait_for_apply_ready(page, timeout_ms=15000)


_AURUS_APPLY_STATE_JS = """
() => {
  const btn = document.querySelector('#jobs-desc button');
  if (!btn) return 'missing';

  const chatbotOpen = !!document.querySelector(
    '.chatbot_Drawer .botItem, ._chatBotContainer .botItem, #desktopChatBotContainer .botItem'
  );
  if (btn.querySelector('.animate-successBounce')) return 'loading';
  if (chatbotOpen && btn.disabled) return 'loading';

  const spans = [...btn.querySelectorAll('span')];
  const visibleLabels = [];
  for (const span of spans) {
    const label = (span.textContent || '').trim().toLowerCase();
    if (!label) continue;
    const cls = span.className || '';
    const hidden = cls.includes('translate-y-full') || cls.includes('opacity-0');
    if (!hidden) visibleLabels.push(label);
  }
  const visibleLabel = visibleLabels.join(' ').trim();

  if (/\\bapplied\\b/.test(visibleLabel) && !/quick apply/.test(visibleLabel)) {
    return 'applied';
  }
  if (/quick apply/.test(visibleLabel)) {
    return btn.disabled ? 'loading' : 'ready';
  }
  if (btn.disabled) return 'loading';
  return 'missing';
}
"""


async def _aurus_apply_state(page: Page) -> str:
    try:
        state = await page.evaluate(_AURUS_APPLY_STATE_JS)
        return str(state or "missing")
    except Exception:
        return "missing"


async def _wait_for_apply_ready(page: Page, timeout_ms: int = 15000) -> str:
    """Wait for Quick apply (ready) or a stable Applied state — not a transient disabled button."""
    applied_streak = 0
    polls = max(timeout_ms // 400, 1)
    final_state = "missing"
    for _ in range(polls):
        final_state = await _aurus_apply_state(page)
        if final_state == "ready":
            return "ready"
        if final_state == "applied":
            applied_streak += 1
            if applied_streak >= 3:
                return "applied"
        else:
            applied_streak = 0
        await page.wait_for_timeout(400)
    return final_state


async def _aurus_already_applied(page: Page) -> bool:
    state = await _aurus_apply_state(page)
    if state == "applied":
        return True
    body = (await page.locator("body").inner_text()).lower()
    if "already applied" in body or "you have applied" in body:
        return True
    return False


async def _find_quick_apply_button(page: Page):
    state = await _aurus_apply_state(page)
    if state != "ready":
        return None

    candidates = (
        page.locator("#jobs-desc button").filter(has_text=_QUICK_APPLY_RE),
        page.locator("button.rounded-full").filter(has_text=_QUICK_APPLY_RE),
        page.get_by_role("button", name=_QUICK_APPLY_RE),
    )
    for loc in candidates:
        count = await loc.count()
        for i in range(count):
            btn = loc.nth(i)
            try:
                if not await btn.is_visible():
                    continue
                if not await btn.is_enabled():
                    continue
                return btn
            except PlaywrightTimeout:
                continue
    return None


async def _page_jd(page: Page) -> str:
    loc = page.locator("#jobs-desc")
    if await loc.count() > 0:
        text = (await loc.first.inner_text()).strip()
        if len(text) > 120:
            return text[:12000]
    return (await page.locator("body").inner_text())[:12000]


def _unanswered_labels(questions: list[dict], answers: dict[str, str]) -> list[str]:
    missing: list[str] = []
    for field in questions:
        label = field.get("label", "").strip()
        if not label:
            continue
        if answers.get(label, "").strip():
            continue
        missing.append(label)
    return missing


def _queue_missing(
    config: AppConfig,
    job: JobListing,
    questions: list[dict],
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
            source="naukri",
            job_title=job.title,
            company=job.company,
            job_url=job.url,
            labels=missing,
            fields_by_label=fields_by_label,
        )
    return missing


async def _dismiss_overlays(page: Page) -> None:
    for label in ("Skip", "Not now", "Close", "Maybe later", "No thanks"):
        btn = page.get_by_role("button", name=re.compile(label, re.I))
        if await btn.count() > 0:
            try:
                await btn.first.click(timeout=2000)
                await page.wait_for_timeout(500)
            except PlaywrightTimeout:
                pass


async def _fill_cover_letter_if_present(page: Page, note: str) -> None:
    for selector in (
        'textarea[placeholder*="cover" i]',
        'textarea[placeholder*="message" i]',
        'textarea[name*="cover" i]',
        "textarea",
    ):
        area = page.locator(selector)
        if await area.count() > 0 and await area.first.is_visible():
            await area.first.fill(note)
            return


async def _handle_chatbot_questions(
    page: Page,
    job: JobListing,
    config: AppConfig,
    jd: str,
) -> bool | None:
    """Answer Naukri quick-apply chatbot questions. True=done, False=failed, None=skipped."""
    if not await wait_for_chatbot(page, timeout_ms=25000):
        for _ in range(8):
            await page.wait_for_timeout(500)
            if await wait_for_chatbot(page, timeout_ms=4000):
                break
            if await _aurus_already_applied(page):
                return True
        else:
            if not await chatbot_is_open(page):
                return True
            logger.warning("Naukri chatbot open but question panel did not load: %s", job.title)
            return False
    else:
        logger.info("Naukri chatbot question panel ready for %s", job.title)

    await prepare_interactive_page(page, fast=False)
    stable_empty = 0
    for step in range(_MAX_CHATBOT_STEPS):
        if await _aurus_already_applied(page):
            return True

        questions = await discover_naukri_chatbot_questions(page)
        if not questions:
            if not await chatbot_is_open(page):
                return True
            stable_empty += 1
            if stable_empty >= 4:
                logger.warning(
                    "Naukri chatbot stuck with no parseable question: %s",
                    job.title,
                )
                return False
            await page.wait_for_timeout(1500)
            continue

        stable_empty = 0
        logger.info(
            "Naukri: %d chatbot question(s) for %s (step %d)",
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
                source="naukri",
                title=job.title,
                company=job.company,
                url=job.url,
                reason="need answers",
                questions=missing,
            )
            logger.warning(
                "Skipped Naukri apply (need answers): %s — %d question(s) queued",
                job.title,
                len(missing),
            )
            return None

        await prepare_interactive_page(page, fast=False)
        for field in questions:
            label = field.get("label", "").strip()
            answer = answers.get(label, "").strip()
            if not answer:
                continue
            if not await fill_naukri_chatbot_question(page, field, answer):
                _queue_missing(config, job, [field], answers)
                logger.warning("Skipped Naukri apply (could not fill): %s", job.title)
                return None

        await page.wait_for_timeout(1500)

    if await _aurus_already_applied(page):
        return True

    if await chatbot_is_open(page):
        logger.warning("Naukri chatbot still open after questions: %s", job.url)
        return False

    return True


async def _try_apply_on_page(page: Page, job: JobListing, config: AppConfig) -> bool | None:
    await _dismiss_overlays(page)

    state = await _wait_for_apply_ready(page, timeout_ms=12000)
    if state == "applied":
        logger.info("Already applied on Naukri: %s", job.title)
        save_applied_job(
            config.applied_jobs_path,
            job_key("naukri", job.job_id),
            {"source": "naukri", "title": job.title, "url": page.url, "status": "already_applied"},
        )
        return None

    apply_btn = await _find_quick_apply_button(page)
    if apply_btn is None:
        state = await _aurus_apply_state(page)
        if state == "applied":
            logger.info("Already applied on Naukri: %s", job.title)
            return None
        if state == "loading":
            state = await _wait_for_apply_ready(page, timeout_ms=10000)
            if state == "ready":
                apply_btn = await _find_quick_apply_button(page)
            elif state == "applied":
                logger.info("Already applied on Naukri: %s", job.title)
                return None
        if apply_btn is None:
            logger.warning(
                "No apply button on Naukri: %s (url=%s, state=%s)",
                job.title,
                page.url,
                state,
            )
            return False

    if config.application.dry_run:
        logger.info("[DRY RUN] Would apply on Naukri: %s", job.title)
        return None

    jd = await _page_jd(page)
    note = await build_cover_letter(config, job=job, page=page)

    try:
        await apply_btn.click(timeout=8000)
    except PlaywrightTimeout:
        if await _aurus_already_applied(page):
            logger.info("Already applied on Naukri: %s", job.title)
            return None
        logger.warning("Quick apply click timed out for: %s", job.title)
        return False

    for _ in range(30):
        if await chatbot_is_open(page):
            break
        state = await _aurus_apply_state(page)
        if state in ("loading", "applied"):
            break
        await page.wait_for_timeout(300)

    await _fill_cover_letter_if_present(page, note)

    chatbot_result = await _handle_chatbot_questions(page, job, config, jd)
    if chatbot_result is None:
        return None
    if chatbot_result is False:
        logger.warning("Could not complete Naukri chatbot for: %s", job.title)
        return False

    if not await _aurus_already_applied(page):
        await page.wait_for_timeout(2000)
    if not await _aurus_already_applied(page):
        logger.warning("Could not confirm Naukri apply for: %s", job.title)
        return False

    save_applied_job(
        config.applied_jobs_path,
        job_key("naukri", job.job_id),
        {"source": "naukri", "title": job.title, "url": page.url},
    )
    logger.info("Applied on Naukri: %s", job.title)
    return True


async def apply_to_job(page: Page, job: JobListing, config: AppConfig) -> bool:
    if not job.easy_apply:
        logger.info("Skipping Naukri job without quick apply: %s", job.title)
        return False
    if "job-listings" not in job.url:
        logger.warning("Skipping Naukri job without detail URL: %s", job.title)
        return False

    await goto_settled(page, _job_detail_url(job.url))
    await _wait_for_job_detail(page)
    result = await _try_apply_on_page(page, job, config)
    return result is True


async def apply_batch(page: Page, _context, jobs: list[JobListing], config: AppConfig) -> int:
    applied = 0
    for job in jobs:
        try:
            if await apply_to_job(page, job, config):
                applied += 1
        except Exception:
            logger.exception("Naukri apply failed: %s", job.url)
        delay = random.randint(config.application.delay_seconds_min, config.application.delay_seconds_max)
        await page.wait_for_timeout(delay * 1000)
    return applied
