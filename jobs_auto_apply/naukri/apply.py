from __future__ import annotations

import logging
import re

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from ..apply_runner import run_apply_batch
from ..application_questions import resolve_question_answers
from ..page_load import prepare_interactive_page
from ..config import AppConfig
from ..cover_letter import build_cover_letter
from ..pending_questions import queue_unanswered
from ..utils import JobListing, defer_job_for_run, job_key, save_applied_job
from .questions import (
    CannotAnswerTruthfully,
    chatbot_is_open,
    discover_naukri_chatbot_questions,
    fill_naukri_chatbot_question,
    wait_for_chatbot,
    _chatbot_flow_complete,
)

logger = logging.getLogger("job_apply")

_AURUS_JD_SELECTOR = '#jobs-desc, [style*="view-transition-name: job-title"]'
_QUICK_APPLY_RE = re.compile(r"quick apply", re.I)
_NON_QUICK_DETAIL_RE = re.compile(
    r"\b(apply on company|company site|apply on website|apply on web|"
    r"registered consult|walk-?in only|apply via consultant)\b",
    re.I,
)
_ALREADY_APPLIED_BODY_RE = re.compile(
    r"\b(already applied|you have applied|you've applied|you applied|"
    r"application (?:has been )?(?:sent|submitted)|successfully applied|"
    r"applied to this (?:job|role)|applied on)\b",
    re.I,
)
_MAX_CHATBOT_STEPS = 15
_NAUKRI_POST_APPLY_RE = re.compile(
    r"/mnjuser/(?:recommendedjobs|homepage|applyhistory|myapply)(?:[/?#]|$)",
    re.I,
)


def _is_naukri_post_apply_url(url: str) -> bool:
    return bool(_NAUKRI_POST_APPLY_RE.search(url))


_SIMILAR_OPPORTUNITIES_JS = """
() => {
  const text = (document.body?.innerText || '').toLowerCase();
  if (!text.includes('similar opportunities')) return false;
  const jd = document.querySelector('#jobs-desc');
  if (!jd) return true;
  const btn = jd.querySelector('button');
  if (!btn) return true;
  return !/quick apply/i.test(btn.innerText || '');
}
"""


async def _naukri_similar_opportunities_page(page: Page) -> bool:
    """True when Naukri shows the post-apply similar-jobs feed instead of job detail."""
    try:
        return bool(await page.evaluate(_SIMILAR_OPPORTUNITIES_JS))
    except Exception:
        return False


async def _naukri_post_apply_redirected(page: Page) -> bool:
    try:
        if _is_naukri_post_apply_url(page.url):
            return True
        return await _naukri_similar_opportunities_page(page)
    except Exception:
        return False


async def _stabilize_after_naukri_apply(page: Page) -> None:
    """Naukri often navigates to recommended jobs after apply — dismiss lingering UI."""
    if not await _naukri_post_apply_redirected(page):
        return
    await _dismiss_overlays(page)
    for selector in (
        ".chatbot_Overlay.show",
        ".chatbot_Drawer .close",
        ".chatbot_Drawer [class*='close']",
        "button[aria-label*='close' i]",
    ):
        loc = page.locator(selector)
        if await loc.count() > 0:
            try:
                await loc.first.click(timeout=1500)
                await page.wait_for_timeout(300)
            except PlaywrightTimeout:
                pass
    try:
        await page.goto("about:blank", wait_until="domcontentloaded", timeout=8000)
    except Exception:
        pass


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
  const jd = document.querySelector('#jobs-desc');

  function appliedInJobDesc() {
    if (!jd) return false;
    const text = (jd.innerText || '').toLowerCase();
    if (/quick apply/.test(text)) return false;
    return (
      /\\byou (have )?applied\\b|\\balready applied\\b|\\bapplication (has been )?(sent|submitted)\\b|\\bapplied on\\b|\\bsuccessfully applied\\b/.test(text)
    );
  }

  const btn = jd ? jd.querySelector('button') : null;
  if (!btn) {
    return appliedInJobDesc() ? 'applied' : 'missing';
  }

  const chatbotOpen = !!document.querySelector(
    '.chatbot_Drawer .botItem, ._chatBotContainer .botItem, #desktopChatBotContainer .botItem'
  );
  if (btn.querySelector('.animate-successBounce')) return 'loading';
  if (chatbotOpen && btn.disabled) return 'loading';

  // The Aurus apply button stacks 3 animated layers (Quick apply / blank /
  // Applied); only the one at rest (translate-y-0) is actually on screen. The
  // hidden layers slide away with -translate-y-[200%] / translate-y-full, so we
  // must treat ANY non-zero translate-y as hidden — not just "translate-y-full".
  const spans = [...btn.querySelectorAll('span')];
  const layerSpans = spans.filter((s) => (s.className || '').includes('translate-y-'));
  let visibleLabel = '';
  if (layerSpans.length) {
    visibleLabel = layerSpans
      .filter((s) => {
        const cls = s.className || '';
        return cls.includes('translate-y-0') && !cls.includes('opacity-0');
      })
      .map((s) => (s.textContent || '').trim().toLowerCase())
      .filter(Boolean)
      .join(' ')
      .trim();
  } else {
    const visibleLabels = [];
    for (const span of spans) {
      const label = (span.textContent || '').trim().toLowerCase();
      if (!label) continue;
      const cls = span.className || '';
      const hidden = cls.includes('translate-y-full') || cls.includes('opacity-0');
      if (!hidden) visibleLabels.push(label);
    }
    visibleLabel = visibleLabels.join(' ').trim();
  }

  if (/quick apply/.test(visibleLabel)) {
    return btn.disabled ? 'loading' : 'ready';
  }
  if (/\\bapplied\\b/.test(visibleLabel)) {
    return 'applied';
  }
  if (btn.disabled && appliedInJobDesc()) return 'applied';
  if (btn.disabled) return 'loading';
  if (appliedInJobDesc()) return 'applied';
  return 'missing';
}
"""


async def _naukri_detail_is_non_quick_apply(page: Page) -> bool:
    """True when job detail clearly shows external / non-quick apply."""
    jd = page.locator("#jobs-desc")
    if await jd.count() == 0:
        return False
    try:
        text = (await jd.first.inner_text()).lower()
    except Exception:
        return False
    if _NON_QUICK_DETAIL_RE.search(text):
        return True
    if "quick apply" in text:
        return False
    btn = jd.locator("button").first
    if await btn.count() > 0:
        try:
            label = (await btn.inner_text()).strip().lower()
            if label and "quick apply" not in label and "applied" not in label:
                return True
        except PlaywrightTimeout:
            pass
    state = await _aurus_apply_state(page)
    if state == "missing" and "quick apply" not in text:
        return True
    return False


async def _aurus_apply_state(page: Page) -> str:
    try:
        state = await page.evaluate(_AURUS_APPLY_STATE_JS)
        return str(state or "missing")
    except Exception:
        return "missing"


async def _wait_for_apply_ready(page: Page, timeout_ms: int = 15000) -> str:
    """Wait for Quick apply (ready) or a stable Applied state — not a transient disabled button."""
    applied_streak = 0
    polls = max(timeout_ms // 250, 1)
    final_state = "missing"
    for _ in range(polls):
        final_state = await _aurus_apply_state(page)
        if final_state == "ready":
            return "ready"
        if final_state == "applied":
            if await _find_quick_apply_button(page) is not None:
                return "ready"
            applied_streak += 1
            if applied_streak >= 2:
                return "applied"
        else:
            applied_streak = 0
        await page.wait_for_timeout(250)
    return final_state


async def _aurus_already_applied(page: Page) -> bool:
    """True when Quick Apply is gone and the job detail shows a real Applied state."""
    if await _find_quick_apply_button(page) is not None:
        return False
    state = await _aurus_apply_state(page)
    if state == "ready":
        return False
    if state != "applied":
        return False
    # state == "applied" already means the button's on-screen layer reads
    # "Applied". Do NOT reject just because "quick apply" appears in #jobs-desc
    # innerText — the off-screen (translated-away) button layer always contains
    # that text, which previously caused already-applied jobs to be retried.
    jd = page.locator("#jobs-desc")
    if await jd.count() > 0:
        jd_text = (await jd.first.inner_text()).lower()
        if _ALREADY_APPLIED_BODY_RE.search(jd_text):
            return True
    return True


async def _confirm_already_applied_on_site(page: Page) -> bool:
    """Require consecutive Applied reads — Quick Apply button overrides."""
    streak = 0
    for _ in range(6):
        if await _find_quick_apply_button(page) is not None:
            return False
        if await _aurus_already_applied(page):
            streak += 1
            if streak >= 2:
                return True
        else:
            streak = 0
        await page.wait_for_timeout(300)
    return False


def _log_already_applied(
    job: JobListing, *, reason: str = "", config: AppConfig | None = None
) -> None:
    if reason:
        logger.info("Already applied on Naukri (%s): %s", reason, job.title)
    else:
        logger.info("Already applied on Naukri: %s", job.title)
    # A job we've already applied to should not linger as a technical failure.
    if config is not None and getattr(job, "job_id", ""):
        try:
            from ..technical_failures import clear_technical_failure

            clear_technical_failure(config.base_dir, job_key(job.source, job.job_id))
        except Exception:  # noqa: BLE001 - best-effort cleanup, never block apply flow
            pass


async def _confirm_naukri_apply(page: Page, job: JobListing, config: AppConfig) -> bool:
    """Detect Applied state after chatbot — drawer close, redirect, or longer poll."""
    if await _naukri_post_apply_redirected(page):
        logger.info(
            "Naukri apply confirmed (post-apply page): %s — %s",
            job.title,
            page.url,
        )
        await _stabilize_after_naukri_apply(page)
        return True

    if await _aurus_already_applied(page):
        return True

    for _ in range(40):
        if await _naukri_post_apply_redirected(page):
            logger.info(
                "Naukri apply confirmed (redirected): %s — %s",
                job.title,
                page.url,
            )
            await _stabilize_after_naukri_apply(page)
            return True
        if await _aurus_already_applied(page):
            return True
        await page.wait_for_timeout(150)

    if not await chatbot_is_open(page):
        await page.wait_for_timeout(600)
        if await _naukri_post_apply_redirected(page):
            logger.info(
                "Naukri apply confirmed (redirect after chatbot): %s",
                job.title,
            )
            await _stabilize_after_naukri_apply(page)
            return True
        if await _aurus_already_applied(page):
            return True
        try:
            body = (await page.locator("body").inner_text()).lower()
        except Exception:
            body = ""
        if any(
            phrase in body
            for phrase in (
                "successfully applied",
                "application submitted",
                "you have applied",
                "applied successfully",
            )
        ):
            return True
        logger.warning(
            "Naukri apply not confirmed (chatbot closed): %s",
            job.title,
        )
        return False

    if await _chatbot_flow_complete(page):
        await page.wait_for_timeout(400)
        if await _naukri_post_apply_redirected(page):
            logger.info(
                "Naukri apply confirmed (redirect after chatbot flow): %s",
                job.title,
            )
            await _stabilize_after_naukri_apply(page)
            return True
        if await _aurus_already_applied(page):
            return True

    if await _naukri_post_apply_redirected(page):
        await _stabilize_after_naukri_apply(page)
        return True

    return False


async def _find_quick_apply_button(page: Page):
    state = await _aurus_apply_state(page)
    if state != "ready":
        return None

    candidates = (
        page.locator("#jobs-desc button").filter(has_text=_QUICK_APPLY_RE),
        page.locator("#jobs-desc button.rounded-full").filter(has_text=_QUICK_APPLY_RE),
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
    *,
    reason: str = "need answers",
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
        defer_job_for_run(config.applied_jobs_path, job, reason=reason)
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
    step_delay = config.application.platform_delays.naukri_chatbot_step_ms
    if not await wait_for_chatbot(page, timeout_ms=20000):
        try:
            await page.wait_for_function(
                """() => !!document.querySelector(
                  '.chatbot_Drawer .botItem, ._chatBotContainer .botItem, #desktopChatBotContainer .botItem'
                )""",
                timeout=8000,
                polling=200,
            )
        except PlaywrightTimeout:
            if await _aurus_already_applied(page):
                return True
            if not await chatbot_is_open(page):
                return True
            logger.warning("Naukri chatbot open but question panel did not load: %s", job.title)
            return False
    else:
        logger.info("Naukri chatbot question panel ready for %s", job.title)

    await prepare_interactive_page(page, fast=True)
    stable_empty = 0
    for step in range(_MAX_CHATBOT_STEPS):
        if await _naukri_post_apply_redirected(page):
            return True
        if await _aurus_already_applied(page):
            return True

        questions = await discover_naukri_chatbot_questions(page, config=config)
        if not questions:
            if not await chatbot_is_open(page):
                return True
            if await _chatbot_flow_complete(page):
                return True
            stable_empty += 1
            if stable_empty >= 4:
                logger.warning(
                    "Naukri chatbot stuck with no parseable question: %s",
                    job.title,
                )
                return False
            await page.wait_for_timeout(step_delay)
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

        for field in questions:
            label = field.get("label", "").strip()
            answer = answers.get(label, "").strip()
            if not answer:
                continue
            try:
                filled = await fill_naukri_chatbot_question(page, field, answer, config=config)
                if not filled:
                    # The answer resolved fine, so a miss here is almost always a
                    # timing/animation race under high worker concurrency (chip/radio
                    # panel re-rendering). Re-discover the live question and retry a
                    # couple of times before giving up on the whole application.
                    for _ in range(2):
                        await page.wait_for_timeout(600)
                        if await _aurus_already_applied(page):
                            return True
                        fresh = await discover_naukri_chatbot_questions(page, config=config)
                        refreshed = next(
                            (f for f in fresh if f.get("label", "").strip() == label), None
                        )
                        target_field = refreshed or field
                        if await fill_naukri_chatbot_question(
                            page, target_field, answer, config=config
                        ):
                            filled = True
                            break
            except CannotAnswerTruthfully as exc:
                # Honest skip: the only options would overstate experience the user
                # lacks. Queue for manual decision; do NOT record a technical failure.
                _queue_missing(
                    config, job, [field], answers, reason="cannot answer truthfully"
                )
                logger.info(
                    "Skipped Naukri apply (cannot answer truthfully): %s — %s",
                    job.title,
                    exc.label[:60],
                )
                return None
            if not filled:
                _queue_missing(config, job, [field], answers, reason="could not fill")
                from ..technical_failures import record_technical_failure

                record_technical_failure(
                    config.base_dir,
                    job_key=job_key(job.source, job.job_id),
                    source="naukri",
                    title=job.title,
                    company=job.company,
                    url=job.url,
                    reason=f"could not fill: {label[:80]}",
                )
                logger.warning("Skipped Naukri apply (could not fill): %s", job.title)
                return None

        await page.wait_for_timeout(step_delay)

    if await _aurus_already_applied(page):
        return True

    if await chatbot_is_open(page):
        logger.warning("Naukri chatbot still open after questions: %s", job.url)
        return False

    return True


async def _try_apply_on_page(page: Page, job: JobListing, config: AppConfig) -> bool | None:
    await _dismiss_overlays(page)

    if await _naukri_detail_is_non_quick_apply(page):
        logger.info("Skipping non-quick-apply Naukri job: %s", job.title)
        return None

    await _wait_for_apply_ready(page, timeout_ms=12000)
    apply_btn = await _find_quick_apply_button(page)
    if apply_btn is None:
        state = await _aurus_apply_state(page)
        if state == "loading":
            state = await _wait_for_apply_ready(page, timeout_ms=10000)
            apply_btn = await _find_quick_apply_button(page)
        if apply_btn is None and await _confirm_already_applied_on_site(page):
            _log_already_applied(job, config=config)
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
    note = await build_cover_letter(config, job=job, page=page, jd=jd)

    try:
        await apply_btn.click(timeout=8000)
    except PlaywrightTimeout:
        if await _confirm_already_applied_on_site(page):
            _log_already_applied(job, config=config)
            return None
        logger.warning("Quick apply click timed out for: %s", job.title)
        return False

    try:
        await page.wait_for_function(
            """() => {
              const btn = document.querySelector('#jobs-desc button');
              if (!btn) return false;
              const chatbotOpen = !!document.querySelector(
                '.chatbot_Drawer .botItem, ._chatBotContainer .botItem, #desktopChatBotContainer .botItem'
              );
              if (chatbotOpen) return true;
              const spans = [...btn.querySelectorAll('span')];
              for (const span of spans) {
                const label = (span.textContent || '').trim().toLowerCase();
                const cls = span.className || '';
                // A layer is on screen only at translate-y-0; any other
                // translate-y (e.g. -translate-y-[200%]) means it's slid away.
                const hidden = (cls.includes('translate-y-') && !cls.includes('translate-y-0'))
                  || cls.includes('opacity-0');
                if (!hidden && /quick apply|applied/.test(label)) return true;
              }
              return !!btn.querySelector('.animate-successBounce');
            }""",
            timeout=8000,
            polling=200,
        )
    except PlaywrightTimeout:
        pass

    await _fill_cover_letter_if_present(page, note)

    chatbot_result = await _handle_chatbot_questions(page, job, config, jd)
    if chatbot_result is None:
        return None
    if chatbot_result is False:
        logger.warning("Could not complete Naukri chatbot for: %s", job.title)
        return False

    if not await _confirm_naukri_apply(page, job, config):
        if await _naukri_post_apply_redirected(page):
            logger.info(
                "Naukri apply confirmed on post-apply page (late): %s",
                job.title,
            )
            await _stabilize_after_naukri_apply(page)
        else:
            logger.warning("Could not confirm Naukri apply for: %s", job.title)
            return False

    save_applied_job(
        config.applied_jobs_path,
        job_key("naukri", job.job_id),
        {"source": "naukri", "title": job.title, "url": page.url},
        status="applied",
    )
    logger.info("Applied on Naukri: %s", job.title)
    await _stabilize_after_naukri_apply(page)
    return True


async def goto_naukri_job_detail(page: Page, url: str) -> None:
    """Light navigation — skip full goto_settled for Naukri SPA job detail."""
    await page.goto(_job_detail_url(url), wait_until="domcontentloaded", timeout=45_000)
    try:
        await page.wait_for_selector(_AURUS_JD_SELECTOR, timeout=10_000)
    except PlaywrightTimeout:
        pass
    await page.wait_for_timeout(100)


async def apply_to_job(
    page: Page,
    _context: BrowserContext | None,
    job: JobListing,
    config: AppConfig,
) -> bool | None:
    if not job.easy_apply:
        logger.info("Skipping Naukri job without quick apply: %s", job.title)
        return False
    if "job-listings" not in job.url:
        logger.warning("Skipping Naukri job without detail URL: %s", job.title)
        return False

    await goto_naukri_job_detail(page, job.url)

    if await _naukri_post_apply_redirected(page):
        reason = (
            "similar opportunities page"
            if await _naukri_similar_opportunities_page(page)
            else "post-apply redirect"
        )
        _log_already_applied(job, reason=reason, config=config)
        await _stabilize_after_naukri_apply(page)
        return None

    await _wait_for_job_detail(page)

    if await _find_quick_apply_button(page) is not None:
        if await _naukri_detail_is_non_quick_apply(page):
            logger.info("Skipping non-quick-apply Naukri job on detail: %s", job.title)
            return None
        return await _try_apply_on_page(page, job, config)

    if await _confirm_already_applied_on_site(page):
        _log_already_applied(job, config=config)
        return None

    if await _naukri_detail_is_non_quick_apply(page):
        logger.info("Skipping non-quick-apply Naukri job on detail: %s", job.title)
        return None

    result = await _try_apply_on_page(page, job, config)
    if result is True:
        return True
    if result is None:
        return None
    return False


async def apply_batch(
    page: Page,
    context: BrowserContext | None,
    jobs: list[JobListing],
    config: AppConfig,
) -> int:
    workers = max(1, config.application.naukri_apply_workers)
    if workers > 1:
        logger.info("Naukri parallel apply: %d workers, %d jobs", workers, len(jobs))
    return await run_apply_batch(
        jobs,
        config,
        page,
        context,
        apply_to_job,
        workers=workers,
    )
