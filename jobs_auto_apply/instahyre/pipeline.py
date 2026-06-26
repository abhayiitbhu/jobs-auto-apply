"""Instahyre parallel apply — one tab scrolls feeds while worker tabs apply concurrently.

Instahyre is feed/position based (no per-job URL), so the producer activates each
feed once, captures its fully-filtered URL, and streams the visible employer rows
into a queue. Worker tabs reopen that same filtered URL, locate each job's row by
name, open it and click Apply. Because already-applied rows stay in the DOM (only
their button hides), reopening the feed in a fresh tab keeps the listing stable.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from playwright.async_api import BrowserContext, Page

from ..apply_filters import filter_pending_jobs
from ..apply_runner import (
    ApplyBatchStopped,
    _apply_job_with_retries,
    is_target_closed_error,
)
from ..config import AppConfig
from ..limits import apply_cap
from ..utils import JobListing, job_key, load_applied_jobs, save_applied_job
from .apply import _click_apply, _has_apply_button, _instahyre_delay_ms
from .feeds import (
    InstahyreFeedSpec,
    _dismiss_apply_modal,
    _wait_for_opportunities,
    activate_feed,
    feeds_from_config,
)
from .search import _EXTRACT_JS, EMPLOYER_ROW, job_id_from_card

logger = logging.getLogger("job_apply")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


async def _click_view_for_name(page: Page, full_name: str, *, max_scrolls: int = 40) -> bool:
    """Find the employer row matching ``full_name`` (scrolling to load it) and open it."""
    if not full_name:
        return False
    for _ in range(max_scrolls):
        row = page.locator(EMPLOYER_ROW).filter(has_text=full_name).first
        if await row.count() > 0:
            btn = row.locator("#interested-btn, button.button-interested")
            if await btn.count() > 0:
                try:
                    await btn.first.scroll_into_view_if_needed(timeout=2000)
                    await btn.first.click(timeout=5000)
                    return True
                except Exception:
                    pass
            link = row.locator("a#employer-profile-opportunity, a.row.text-link")
            if await link.count() > 0:
                try:
                    await link.first.click(timeout=5000)
                    return True
                except Exception:
                    pass
            return False
        prev = await page.locator(EMPLOYER_ROW).count()
        await page.mouse.wheel(0, 2400)
        await page.wait_for_timeout(500)
        if await page.locator(EMPLOYER_ROW).count() == prev:
            break
    return False


async def _apply_in_feed(
    page: Page,
    _context: BrowserContext | None,
    job: JobListing,
    config: AppConfig,
) -> bool | None:
    """Worker apply: open the job's row on its feed and click Apply."""
    feed_url = str(job.meta.get("feed_url") or "")
    full_name = str(job.meta.get("full_name") or "").strip()
    if not full_name:
        full_name = f"{job.company} - {job.title}".strip(" -")

    if feed_url and feed_url not in page.url:
        await page.goto(feed_url, wait_until="domcontentloaded", timeout=90000)
        await _wait_for_opportunities(page)

    if not await _click_view_for_name(page, full_name):
        logger.warning("Instahyre: could not open row for %s @ %s", job.title, job.company or "?")
        return False

    await page.wait_for_timeout(_instahyre_delay_ms(config))

    if not await _has_apply_button(page):
        await _dismiss_apply_modal(page)
        return False
    if not await _click_apply(page):
        await _dismiss_apply_modal(page)
        return False

    save_applied_job(
        config.applied_jobs_path,
        job_key("instahyre", job.job_id),
        {"source": "instahyre", "title": job.title, "company": job.company, "url": feed_url},
    )
    logger.info("Applied on Instahyre: %s @ %s", job.title, job.company or "?")
    await _dismiss_apply_modal(page)
    return True


async def run_instahyre_pipeline(
    page: Page,
    context: BrowserContext,
    config: AppConfig,
    applied_ids: set[str],
    *,
    search_urls: list[str] | None = None,
    feed_dicts: list[dict] | None = None,
    default_job_functions: list[str] | None = None,
) -> int:
    """Stream feed rows into a queue; worker tabs apply in parallel."""
    workers_n = max(1, config.application.instahyre_apply_workers)
    cap = apply_cap(config.application.max_jobs_per_run)
    max_attempts = 1 + max(0, config.application.apply_retries)
    backoff_ms = max(0, config.application.retry_backoff_ms)

    specs = feeds_from_config(
        search_urls=search_urls,
        feed_dicts=feed_dicts,
        default_job_functions=default_job_functions,
    )

    queue: asyncio.Queue[JobListing | None] = asyncio.Queue(maxsize=workers_n * 4)
    seen: set[str] = set()
    stats = {"queued": 0, "applied": 0, "failed": 0, "skipped": 0}
    stats_lock = asyncio.Lock()
    stop = asyncio.Event()

    async def _produce_feed(spec: InstahyreFeedSpec) -> None:
        feed_key = await activate_feed(page, spec)
        feed_url = page.url
        stable = 0
        last_count = 0
        for _round in range(40):
            if stop.is_set():
                return
            if cap is not None and stats["queued"] >= cap:
                stop.set()
                return

            try:
                rows = await page.evaluate(_EXTRACT_JS)
            except Exception as exc:
                if is_target_closed_error(exc):
                    stop.set()
                    return
                raise

            listings: list[JobListing] = []
            for row in rows or []:
                title = str(row.get("title", "")).strip() or "Unknown"
                company = str(row.get("company", "")).strip()
                full_name = str(row.get("full_name", "")).strip()
                jid = job_id_from_card(title, company, feed_key)
                if jid in seen:
                    continue
                seen.add(jid)
                listings.append(
                    JobListing(
                        job_id=jid,
                        title=title,
                        company=company,
                        url=feed_url,
                        source="instahyre",
                        easy_apply=True,
                        meta={
                            "feed_url": feed_url,
                            "feed_key": feed_key,
                            "full_name": full_name,
                            "card_index": int(row.get("card_index", 0)),
                        },
                    )
                )

            if listings:
                current_applied = load_applied_jobs(config.applied_jobs_path)
                pending = filter_pending_jobs(listings, current_applied, 0, config)
                if config.application.dry_run:
                    for job in pending:
                        logger.info(
                            "[DRY RUN] Would apply on Instahyre: %s @ %s",
                            job.title,
                            job.company or "?",
                        )
                else:
                    for job in pending:
                        if stop.is_set():
                            break
                        if cap is not None and stats["queued"] >= cap:
                            stop.set()
                            break
                        await queue.put(job)
                        async with stats_lock:
                            stats["queued"] += 1

            count = len(rows or [])
            if count == last_count:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
                last_count = count

            try:
                await page.mouse.wheel(0, 2400)
                await page.wait_for_timeout(600)
            except Exception as exc:
                if is_target_closed_error(exc):
                    stop.set()
                    return
                raise

    async def producer() -> None:
        try:
            for spec in specs:
                if stop.is_set():
                    break
                if cap is not None and stats["queued"] >= cap:
                    break
                await _produce_feed(spec)
        finally:
            for _ in range(workers_n):
                await queue.put(None)

    async def worker(worker_id: int) -> None:
        try:
            tab = await context.new_page()
        except Exception as exc:
            if is_target_closed_error(exc):
                stop.set()
                return
            raise
        label = f"[w{worker_id}]"
        try:
            while not stop.is_set():
                job = await queue.get()
                if job is None:
                    break
                logger.info("%s Applying: %s @ %s", label, job.title, job.company or "?")
                try:
                    ok = await _apply_job_with_retries(
                        job,
                        config,
                        tab,
                        context,
                        _apply_in_feed,
                        max_attempts=max_attempts,
                        retry_backoff_ms=backoff_ms,
                        label=label,
                    )
                except ApplyBatchStopped:
                    logger.error("%s Stopping pipeline — browser/page was closed", label)
                    stop.set()
                    break
                async with stats_lock:
                    if ok is True:
                        stats["applied"] += 1
                    elif ok is None:
                        stats["skipped"] += 1
                    else:
                        stats["failed"] += 1
        finally:
            with contextlib.suppress(Exception):
                await tab.close()

    logger.info(
        "Instahyre pipeline: %d workers — scroll feeds and apply in parallel",
        workers_n,
    )
    producer_task = asyncio.create_task(producer())
    worker_tasks = [asyncio.create_task(worker(i + 1)) for i in range(workers_n)]
    prod_result, *_ = await asyncio.gather(producer_task, return_exceptions=True)
    if isinstance(prod_result, BaseException):
        stop.set()
        if not is_target_closed_error(prod_result):
            logger.warning(
                "Instahyre pipeline producer error: %s: %s",
                type(prod_result).__name__,
                prod_result,
            )
    worker_results = await asyncio.gather(*worker_tasks, return_exceptions=True)
    for r in worker_results:
        if isinstance(r, BaseException) and not isinstance(r, ApplyBatchStopped):
            logger.warning("Instahyre pipeline worker error: %s: %s", type(r).__name__, r)

    logger.info(
        "Instahyre pipeline finished: %d applied, %d skipped, %d failed (%d queued)",
        stats["applied"],
        stats["skipped"],
        stats["failed"],
        stats["queued"],
    )
    return stats["applied"]
