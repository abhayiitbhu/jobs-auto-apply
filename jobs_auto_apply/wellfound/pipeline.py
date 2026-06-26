from __future__ import annotations

import asyncio
import logging

from playwright.async_api import BrowserContext, Page

from ..config import AppConfig
from ..job_selection import load_applied_companies
from ..limits import apply_cap, scrape_limit
from ..role_filter import filter_skipped_roles
from ..salary import is_job_salary_eligible, parse_salary_ranges
from ..utils import JobListing, company_key, filter_skipped_companies, job_key
from .apply import process_wellfound_job
from .guard import (
    WellfoundAccessRestrictedError,
    WellfoundApplicationLimitReached,
    is_access_restricted,
    pause_between_jobs,
)
from .search import iter_job_listings

logger = logging.getLogger("job_apply")


class CompanyGate:
    """Claim one opening per company while workers run in parallel (first eligible wins)."""

    def __init__(self, config: AppConfig, applied_companies: set[str]) -> None:
        self._one_per = config.application.one_job_per_company
        self._claimed = set(applied_companies)
        self._lock = asyncio.Lock()

    async def try_claim(self, company: str) -> bool:
        if not self._one_per:
            return True
        key = company_key(company)
        if not key:
            return True
        async with self._lock:
            if key in self._claimed:
                return False
            self._claimed.add(key)
            return True

    def release(self, company: str) -> None:
        if not self._one_per:
            return
        key = company_key(company)
        if key:
            self._claimed.discard(key)


class ApplyBudget:
    def __init__(self, cap: int | None) -> None:
        self._cap = cap
        self.applied = 0
        self._lock = asyncio.Lock()

    async def can_apply(self) -> bool:
        if self._cap is None:
            return True
        async with self._lock:
            return self.applied < self._cap

    async def record_success(self) -> None:
        async with self._lock:
            self.applied += 1


def _quick_skip(job: JobListing, config: AppConfig, applied_ids: set[str]) -> bool:
    if job_key(job.source, job.job_id) in applied_ids:
        return True
    if not filter_skipped_companies([job], config.profile.skip_companies):
        return True
    if not filter_skipped_roles(
        [job],
        skip_frontend=config.profile.skip_frontend_roles,
        skip_qa_test=config.profile.skip_qa_test_roles,
        keywords=config.profile.skip_role_keywords,
    ):
        return True
    if config.application.skip_ineligible_salary:
        card_text = "\n".join(
            p
            for p in (
                job.meta.get("salary_display", ""),
                job.meta.get("card_text", ""),
                job.title,
            )
            if p
        )
        if parse_salary_ranges(card_text) and not is_job_salary_eligible(
            meta=job.meta,
            modal=card_text,
            min_inr_lpa=config.application.min_inr_salary_lpa,
        ):
            logger.info(
                "Skipping feed salary-ineligible: %s — %s",
                job.title,
                job.meta.get("salary_display", card_text[:60]),
            )
            return True
    return False


async def run_wellfound_pipeline(
    page: Page,
    context: BrowserContext,
    config: AppConfig,
    applied_ids: set[str],
) -> int:
    """
    Stream listings from the search feed while workers process jobs end-to-end.

    One browser tab per worker: open job → enrich → apply → next job.
    The main tab keeps scrolling and feeds the queue.
    """
    workers_n = max(1, config.application.apply_workers)
    limit = scrape_limit(config.application.max_jobs_per_run, multiplier=1)
    cap = apply_cap(config.application.jobs_per_platform)
    if cap is None:
        cap = apply_cap(config.application.max_jobs_per_run)

    queue: asyncio.Queue[JobListing | None] = asyncio.Queue(maxsize=workers_n * 3)
    company_gate = CompanyGate(config, load_applied_companies(config.applied_jobs_path))
    budget = ApplyBudget(cap)
    max_attempts = 1 + max(0, config.application.apply_retries)
    stats = {"queued": 0, "skipped_quick": 0, "applied": 0, "failed": 0, "skipped": 0}
    stats_lock = asyncio.Lock()
    blocked = asyncio.Event()

    async def producer() -> None:
        try:
            async for job in iter_job_listings(page, limit):
                if blocked.is_set():
                    break
                if await is_access_restricted(page):
                    blocked.set()
                    logger.error(
                        "Wellfound blocked this session (Access is temporarily restricted). "
                        "Stop the run, wait 30-60 min, reduce apply_workers to 2-3, "
                        "and set delay_seconds_min/max to 2-5."
                    )
                    break
                if not await budget.can_apply():
                    break
                if _quick_skip(job, config, applied_ids):
                    async with stats_lock:
                        stats["skipped_quick"] += 1
                    continue
                await queue.put(job)
                async with stats_lock:
                    stats["queued"] += 1
        finally:
            for _ in range(workers_n):
                await queue.put(None)

    async def worker(worker_id: int) -> None:
        tab = await context.new_page()
        try:
            while True:
                if blocked.is_set():
                    break
                job = await queue.get()
                if job is None:
                    break
                if not await budget.can_apply():
                    continue

                label = f"[w{worker_id}]"
                result: bool | None = False
                success = False
                for attempt in range(1, max_attempts + 1):
                    try:
                        if await is_access_restricted(tab):
                            blocked.set()
                            raise WellfoundAccessRestrictedError("session blocked")
                        result = await process_wellfound_job(
                            tab,
                            context,
                            job,
                            config,
                            company_gate=company_gate,
                            label=label,
                        )
                    except WellfoundAccessRestrictedError:
                        blocked.set()
                        logger.error("%s Wellfound access restricted — stopping workers.", label)
                        break
                    except WellfoundApplicationLimitReached:
                        blocked.set()
                        logger.warning(
                            "%s Wellfound application limit reached — stopping workers.",
                            label,
                        )
                        break
                    except Exception:
                        if attempt < max_attempts:
                            logger.warning(
                                "%s Error on %s (attempt %d/%d), retrying…",
                                label,
                                job.url,
                                attempt,
                                max_attempts,
                            )
                            await tab.wait_for_timeout(3000)
                            continue
                        logger.exception("%s Failed %s after %d attempts", label, job.url, max_attempts)
                        result = False

                    if result is True:
                        success = True
                        await budget.record_success()
                        async with stats_lock:
                            stats["applied"] += 1
                        break
                    if result is None:
                        async with stats_lock:
                            stats["skipped"] += 1
                        break
                    if attempt < max_attempts:
                        logger.warning(
                            "%s Apply failed %s @ %s (attempt %d/%d), retrying…",
                            label,
                            job.title,
                            job.company or "?",
                            attempt,
                            max_attempts,
                        )
                        await tab.wait_for_timeout(3000)

                if blocked.is_set():
                    break

                if not success and result is False:
                    async with stats_lock:
                        stats["failed"] += 1

                if not blocked.is_set():
                    await pause_between_jobs(tab, config)
        finally:
            await tab.close()

    logger.info(
        "Pipeline: %d workers — scroll feed and apply in parallel (limit %d listings)",
        workers_n,
        limit,
    )
    producer_task = asyncio.create_task(producer())
    worker_tasks = [asyncio.create_task(worker(i + 1)) for i in range(workers_n)]
    await producer_task
    await asyncio.gather(*worker_tasks)

    logger.info(
        "Pipeline finished: %d applied, %d skipped, %d failed (%d queued from feed, %d filtered before queue)",
        stats["applied"],
        stats["skipped"],
        stats["failed"],
        stats["queued"],
        stats["skipped_quick"],
    )
    return stats["applied"]
