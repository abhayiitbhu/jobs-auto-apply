"""Naukri SRP scroll + parallel apply — workers take jobs while the feed keeps loading."""

from __future__ import annotations

import asyncio
import logging

from playwright.async_api import BrowserContext, Page

from ..apply_filters import filter_pending_jobs
from ..apply_runner import ApplyBatchStopped, _apply_job_with_retries, is_target_closed_error
from ..config import AppConfig
from ..limits import scrape_limit
from ..utils import JobListing, load_applied_jobs
from .apply import apply_to_job
from .search import collect_naukri_srp_batch, scroll_naukri_srp_more

logger = logging.getLogger("job_apply")


async def run_naukri_pipeline(
    page: Page,
    context: BrowserContext,
    config: AppConfig,
    applied_ids: set[str],
) -> int:
    """
    Scroll/collect on the main SRP tab; worker tabs apply from a shared queue.
    Frees workers to pick up new listings without waiting for the whole batch.
    """
    filters = config.naukri.filters
    workers_n = max(1, config.application.naukri_apply_workers)
    per_batch_limit = scrape_limit(config.application.max_jobs_per_run, multiplier=3)
    max_batches = max(1, filters.max_pages)
    max_attempts = 1 + max(0, config.application.apply_retries)
    backoff_ms = max(0, config.application.retry_backoff_ms)

    queue: asyncio.Queue[JobListing | None] = asyncio.Queue(maxsize=workers_n * 4)
    session_seen: set[str] = set()
    stats = {"queued": 0, "applied": 0, "failed": 0, "skipped": 0}
    stats_lock = asyncio.Lock()
    stop = asyncio.Event()

    async def producer() -> None:
        nonlocal applied_ids
        # Stream collection: grab whatever cards are already on screen and queue
        # them immediately so workers start applying, then keep scrolling and
        # queueing more. The queue's maxsize provides backpressure, so the
        # producer naturally scrolls ahead only as fast as workers consume.
        scroll_cap = max(max_batches, 50)
        empty_rounds = 0
        try:
            for round_num in range(1, scroll_cap + 1):
                if stop.is_set():
                    break
                if round_num > 1:
                    try:
                        grew = await scroll_naukri_srp_more(page)
                    except Exception as exc:
                        if is_target_closed_error(exc):
                            logger.error(
                                "Naukri pipeline: SRP tab closed during scroll — stopping"
                            )
                            stop.set()
                            break
                        raise
                    if not grew:
                        empty_rounds += 1
                        if empty_rounds >= 3:
                            logger.info(
                                "Naukri pipeline: no more SRP listings after scroll"
                            )
                            break
                        await page.wait_for_timeout(400)

                try:
                    jobs = await collect_naukri_srp_batch(
                        page,
                        per_batch_limit,
                        seen_job_ids=session_seen,
                        quick_apply_only=filters.quick_apply_only,
                        sort=filters.sort,
                        max_job_age_days=filters.max_job_age_days,
                        initial_scroll=False,
                    )
                except Exception as exc:
                    if is_target_closed_error(exc):
                        logger.error(
                            "Naukri pipeline: SRP tab closed — stopping collection"
                        )
                        stop.set()
                        break
                    raise
                for job in jobs:
                    session_seen.add(job.job_id)
                if jobs:
                    empty_rounds = 0
                elif round_num > 1:
                    empty_rounds += 1
                    if empty_rounds >= 3:
                        break
                    continue

                applied_ids = load_applied_jobs(config.applied_jobs_path)
                pending = filter_pending_jobs(
                    jobs, applied_ids, config.application.max_jobs_per_run, config
                )
                if config.application.dry_run:
                    for job in pending:
                        logger.info("[dry-run] Would apply: %s @ %s", job.title, job.company)
                    continue

                for job in pending:
                    if stop.is_set():
                        break
                    await queue.put(job)
                    async with stats_lock:
                        stats["queued"] += 1
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
                logger.info("%s Applying: %s @ %s", label, job.title, job.company)
                try:
                    ok = await _apply_job_with_retries(
                        job,
                        config,
                        tab,
                        context,
                        apply_to_job,
                        max_attempts=max_attempts,
                        retry_backoff_ms=backoff_ms,
                        label=label,
                    )
                except ApplyBatchStopped:
                    # Browser/page closed mid-apply — stop the whole pipeline
                    # rather than letting every worker crash separately.
                    logger.error(
                        "%s Stopping pipeline — browser/page was closed", label
                    )
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
            try:
                await tab.close()
            except Exception:
                pass

    logger.info(
        "Naukri pipeline: %d workers — scroll SRP and apply in parallel",
        workers_n,
    )
    producer_task = asyncio.create_task(producer())
    worker_tasks = [asyncio.create_task(worker(i + 1)) for i in range(workers_n)]
    prod_result, *_ = await asyncio.gather(producer_task, return_exceptions=True)
    if isinstance(prod_result, BaseException):
        stop.set()
        if not is_target_closed_error(prod_result):
            logger.warning(
                "Naukri pipeline producer error: %s: %s",
                type(prod_result).__name__,
                prod_result,
            )
    worker_results = await asyncio.gather(*worker_tasks, return_exceptions=True)
    for r in worker_results:
        if isinstance(r, BaseException) and not isinstance(r, ApplyBatchStopped):
            logger.warning("Naukri pipeline worker error: %s: %s", type(r).__name__, r)

    logger.info(
        "Naukri pipeline finished: %d applied, %d skipped, %d failed (%d jobs processed)",
        stats["applied"],
        stats["skipped"],
        stats["failed"],
        stats["queued"],
    )
    return stats["applied"]
