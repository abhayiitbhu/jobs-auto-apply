from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Optional

from playwright.async_api import BrowserContext, Page

from .config import AppConfig
from .utils import JobListing

logger = logging.getLogger("job_apply")

ApplyFn = Callable[
    [Page, Optional[BrowserContext], JobListing, AppConfig],
    Awaitable[Optional[bool]],
]


class ApplyBatchStopped(Exception):
    """Raised to stop applying to further jobs in a batch."""


async def _apply_job_with_retries(
    job: JobListing,
    config: AppConfig,
    page: Page,
    context: Optional[BrowserContext],
    apply_one: ApplyFn,
    *,
    max_attempts: int,
    label: str = "",
) -> bool:
    prefix = f"{label} " if label else ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = await apply_one(page, context, job, config)
        except ApplyBatchStopped:
            raise
        except Exception:
            if attempt < max_attempts:
                logger.warning(
                    "%sApply error for %s @ %s (attempt %d/%d), retrying...",
                    prefix,
                    job.title,
                    job.company,
                    attempt,
                    max_attempts,
                )
                await page.wait_for_timeout(3000)
                continue
            logger.exception("%sFailed applying to %s after %d attempts", prefix, job.url, max_attempts)
            return False

        if result is True:
            return True
        if result is None:
            return False
        if attempt < max_attempts:
            logger.warning(
                "%sApply failed for %s @ %s (attempt %d/%d), retrying...",
                prefix,
                job.title,
                job.company,
                attempt,
                max_attempts,
            )
            await page.wait_for_timeout(3000)
        else:
            logger.warning(
                "%sSkipping %s @ %s after %d failed attempts",
                prefix,
                job.title,
                job.company,
                max_attempts,
            )
    return False


async def run_apply_batch(
    jobs: list[JobListing],
    config: AppConfig,
    page: Page,
    context: Optional[BrowserContext],
    apply_one: ApplyFn,
) -> int:
    """Apply to jobs. Uses parallel tabs when apply_workers > 1. No delay between jobs."""
    if not jobs:
        return 0

    max_attempts = 1 + max(0, config.application.apply_retries)
    workers = max(1, config.application.apply_workers)

    if workers <= 1 or context is None:
        applied = 0
        for job in jobs:
            try:
                if await _apply_job_with_retries(
                    job, config, page, context, apply_one, max_attempts=max_attempts
                ):
                    applied += 1
            except ApplyBatchStopped as exc:
                logger.warning("%s — stopping apply batch after %d success(es)", exc, applied)
                break
        return applied

    sem = asyncio.Semaphore(workers)
    total = len(jobs)

    async def _one(job: JobListing, index: int) -> bool:
        async with sem:
            tab = await context.new_page()
            label = f"[{index}/{total}]"
            try:
                logger.info("%s Applying: %s @ %s", label, job.title, job.company)
                return await _apply_job_with_retries(
                    job,
                    config,
                    tab,
                    context,
                    apply_one,
                    max_attempts=max_attempts,
                    label=label,
                )
            finally:
                await tab.close()

    results = await asyncio.gather(*[_one(job, i) for i, job in enumerate(jobs, 1)])
    applied = sum(1 for ok in results if ok)
    logger.info("Parallel apply finished: %d/%d succeeded (%d workers)", applied, total, workers)
    return applied
