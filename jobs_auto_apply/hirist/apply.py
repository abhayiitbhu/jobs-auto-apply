from __future__ import annotations

import logging
import re

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from .questions import (
    click_hirist_advance,
    discover_hirist_questions,
    fill_hirist_questions,
    default_checkbox_answer,
    is_hirist_next_enabled,
    hirist_empty_mandatory_fields,
)
from ..page_load import prepare_interactive_page, reveal_footer_actions
from ..application_questions import resolve_question_answers
from ..apply_runner import run_apply_batch
from ..config import AppConfig
from ..pending_questions import queue_unanswered
from ..utils import JobListing, defer_job_for_run, job_key, save_applied_job

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


async def _wait_for_apply_button(page: Page, *, timeout_ms: int = 10_000) -> bool:
    """Wait for Hirist job-detail Apply control (SPA often renders it after domcontentloaded)."""
    try:
        await page.wait_for_function(
            """() => {
              const patterns = [/apply now/i, /quick apply/i, /^apply$/i];
              for (const el of document.querySelectorAll(
                'button, a[role="button"], a, input[type="button"], input[type="submit"]'
              )) {
                const style = window.getComputedStyle(el);
                if (style.display === "none" || style.visibility === "hidden") continue;
                const r = el.getBoundingClientRect();
                if (r.width <= 0 || r.height <= 0) continue;
                const text = (el.innerText || el.value || el.textContent || "").trim();
                if (!text || text.length > 48) continue;
                if (patterns.some((p) => p.test(text))) return true;
              }
              return false;
            }""",
            timeout=timeout_ms,
        )
        return True
    except PlaywrightTimeout:
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


async def _click_advance_button(
    page: Page, *, prep: bool = True, require_enabled: bool = False
) -> str | None:
    """Click Hirist Next / Submit / Confirm."""
    if prep:
        await prepare_interactive_page(page, fast=True)

    if require_enabled and not await is_hirist_next_enabled(page):
        empty = await hirist_empty_mandatory_fields(page)
        if empty:
            preview = "; ".join(str(q)[:40] for q in empty[:3])
            logger.warning(
                "Hirist: Next disabled — not advancing (empty: %s%s)",
                preview,
                " …" if len(empty) > 3 else "",
            )
        return None

    for attempt in range(2):
        clicked = await click_hirist_advance(page)
        if clicked:
            logger.info("Hirist: clicked %s", clicked)
            return clicked
        if attempt == 0:
            await reveal_footer_actions(page, for_form=True)
        await page.wait_for_timeout(250)

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
                    await page.wait_for_timeout(800)
                    return label
                except PlaywrightTimeout:
                    continue
    return None


def _labels_overlap(question_label: str, dom_labels: list[str]) -> bool:
    ql = question_label.strip().lower()
    for dom in dom_labels:
        dl = dom.strip().lower()
        if ql == dl or ql.startswith(dl) or dl.startswith(ql) or ql in dl or dl in ql:
            return True
    return False


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
    *,
    reason: str = "need answers",
    force: bool = False,
) -> list[str]:
    if force:
        # Queue every supplied question even if it has a (non-working) answer —
        # the caller already narrowed this to fields that failed to fill, so the
        # stored answer clearly doesn't fit the form and the user must answer it.
        missing = [
            str(f.get("label", "")).strip()
            for f in questions
            if str(f.get("label", "")).strip()
        ]
    else:
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
        defer_job_for_run(config.applied_jobs_path, job, reason=reason)
    return missing


async def goto_hirist_job_detail(page: Page, url: str) -> None:
    """Light navigation — job pages are SPAs; skip full goto_settled."""
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    try:
        await page.wait_for_selector("main, article, [class*='job']", timeout=10_000)
    except PlaywrightTimeout:
        pass
    await page.wait_for_timeout(300)


async def apply_to_job(
    page: Page,
    _context: BrowserContext | None,
    job: JobListing,
    config: AppConfig,
) -> bool | None:
    await goto_hirist_job_detail(page, job.url)

    if await _already_applied(page):
        logger.info("Already applied on Hirist: %s", job.title)
        return None

    jd = await _page_jd(page)

    if config.application.dry_run:
        logger.info("[DRY RUN] Would apply on Hirist: %s", job.title)
        return None

    if not await _wait_for_apply_button(page):
        if await _already_applied(page):
            logger.info("Already applied on Hirist: %s", job.title)
            return None
        logger.warning("No apply button on Hirist: %s", job.url)
        return False

    if not await _click_apply(page):
        logger.warning("Could not click Apply on Hirist: %s", job.url)
        return False

    try:
        await page.wait_for_selector(
            "text=/Mandatory Question|tell the recruiter more about yourself/i",
            timeout=10_000,
        )
    except PlaywrightTimeout:
        pass
    await prepare_interactive_page(page, fast=True)

    max_steps = 6
    step_delay = config.application.platform_delays.hirist_step_ms
    for step in range(max_steps):
        questions = await discover_hirist_questions(page, prepped=True)
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
            unfilled = await fill_hirist_questions(
                page, questions, answers, prep=False, config=config
            )
            if unfilled:
                # Classify: a field we HAD a usable answer for but still couldn't
                # fill is a DOM/automation problem (technical failure). A field with
                # no usable answer is a content gap → queue for manual answer.
                answered_unfilled = [
                    u for u in unfilled if str(answers.get(u, "")).strip()
                ]
                no_answer = [u for u in unfilled if not str(answers.get(u, "")).strip()]
                if no_answer:
                    # Include labels that aren't in the discovered question list
                    # (undiscovered empty mandatory fields). Without this they'd be
                    # filtered out and silently lost — neither queued nor recorded.
                    q_by_label = {
                        str(q.get("label", "")).strip(): q for q in questions
                    }
                    queue_fields = [
                        q_by_label.get(lbl)
                        or {"label": lbl, "kind": "text", "platform": "hirist"}
                        for lbl in no_answer
                    ]
                    _queue_missing(config, job, queue_fields, answers, force=True)
                    logger.warning(
                        "Hirist: %d field(s) queued for manual answer for %s "
                        "(answer once, then re-run)",
                        len(no_answer),
                        job.title,
                    )
                if answered_unfilled:
                    from ..technical_failures import record_technical_failure

                    record_technical_failure(
                        config.base_dir,
                        job_key=job_key(job.source, job.job_id),
                        source="hirist",
                        title=job.title,
                        company=job.company,
                        url=job.url,
                        reason=f"could not fill {len(answered_unfilled)} answered "
                        "field(s) (selection/DOM failed): "
                        + "; ".join(str(u)[:40] for u in answered_unfilled[:3]),
                    )
                    logger.warning(
                        "Skipped apply (could not fill %d answered field(s)): %s",
                        len(answered_unfilled),
                        job.title,
                    )
                return None

            if questions and not await is_hirist_next_enabled(page):
                empty = await hirist_empty_mandatory_fields(page)
                if empty:
                    logger.warning(
                        "Hirist Next still disabled after fill for %s — empty: %s",
                        job.title,
                        "; ".join(e[:40] for e in empty[:3]),
                    )
                    overlapping = [
                        q for q in questions if _labels_overlap(q["label"], empty)
                    ]
                    # Empty mandatory fields matching no discovered question are a
                    # discovery/automation gap — they can't be queued or answered.
                    undiscovered = [
                        e
                        for e in empty
                        if not any(_labels_overlap(q["label"], [e]) for q in questions)
                    ]
                    # Discovered blockers we had an answer for but stayed empty =
                    # a selection/fill failure (technical); the rest = need answers.
                    answered_empty = [
                        q for q in overlapping
                        if str(answers.get(q["label"], "")).strip()
                    ]
                    no_answer_q = [
                        q for q in overlapping
                        if not str(answers.get(q["label"], "")).strip()
                    ]
                    if no_answer_q:
                        _queue_missing(config, job, no_answer_q, answers, force=True)
                        logger.warning(
                            "Hirist: %d mandatory question(s) queued for manual "
                            "answer for %s (answer once, then re-run)",
                            len(no_answer_q),
                            job.title,
                        )
                    if undiscovered or answered_empty:
                        from ..technical_failures import record_technical_failure

                        blockers = [
                            str(q["label"]) for q in answered_empty
                        ] + undiscovered
                        record_technical_failure(
                            config.base_dir,
                            job_key=job_key(job.source, job.job_id),
                            source="hirist",
                            title=job.title,
                            company=job.company,
                            url=job.url,
                            reason=(
                                f"Next stayed disabled — {len(undiscovered)} "
                                f"undiscovered + {len(answered_empty)} "
                                "answered-but-empty mandatory field(s): "
                            )
                            + "; ".join(b[:40] for b in blockers[:3]),
                        )
                    return None

        if await _application_success(page):
            break

        if questions and not await is_hirist_next_enabled(page):
            await page.wait_for_timeout(800)
            if await _application_success(page):
                break
            logger.warning(
                "Hirist: Next disabled — skipping advance for %s (step %d)",
                job.title,
                step + 1,
            )
            break

        action = await _click_advance_button(
            page, prep=not questions, require_enabled=bool(questions)
        )
        if not action:
            await page.wait_for_timeout(1500)
            if await _application_success(page):
                break
            if questions:
                logger.warning(
                    "Hirist: could not click Next/Submit after filling %d question(s) for %s",
                    len(questions),
                    job.url,
                )
            elif step == 0:
                if await _application_success(page):
                    break
                logger.warning("No Hirist Next/Submit button for %s", job.url)
            break

        if await _application_success(page):
            break

        await page.wait_for_timeout(step_delay)

    if not await _application_success(page):
        await page.wait_for_timeout(1200)
        if await _application_success(page):
            pass
        else:
            logger.warning("Could not confirm Hirist submit for %s", job.url)
            # Genuine technical failure: all questions were filled/queued but the
            # form could not be advanced/submitted (Next/Submit stayed disabled or
            # the success state never appeared). Record it so it's tracked/retried —
            # this path previously returned without logging a technical failure.
            from ..technical_failures import record_technical_failure

            record_technical_failure(
                config.base_dir,
                job_key=job_key(job.source, job.job_id),
                source="hirist",
                title=job.title,
                company=job.company,
                url=job.url,
                reason="could not complete/submit application (Next/Submit not confirmed)",
            )
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
    workers = max(1, config.application.hirist_apply_workers)
    if workers > 1:
        logger.info("Hirist parallel apply: %d workers, %d jobs", workers, len(jobs))
    return await run_apply_batch(
        jobs,
        config,
        page,
        context,
        apply_to_job,
        workers=workers,
    )
