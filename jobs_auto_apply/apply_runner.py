from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from playwright.async_api import BrowserContext, Page

from .config import AppConfig
from .run_issues import record_run_attempt
from .technical_failures import record_technical_failure
from .utils import JobListing, job_key

logger = logging.getLogger("job_apply")

ApplyFn = Callable[
    [Page, BrowserContext | None, JobListing, AppConfig],
    Awaitable[bool | None],
]


class ApplyBatchStopped(Exception):
    """Raised to stop applying to further jobs in a batch."""


class BrowserClosed(ApplyBatchStopped):
    """Raised when the page/context/browser was closed mid-run (e.g. user quit Chrome)."""


def is_target_closed_error(exc: BaseException) -> bool:
    """True when a Playwright error means the page/context/browser is gone."""
    name = type(exc).__name__
    if name in ("TargetClosedError", "TargetClosed"):
        return True
    msg = str(exc).lower()
    return (
        "has been closed" in msg
        or "target page, context or browser" in msg
        or "target closed" in msg
        or "browser has been closed" in msg
    )


async def _apply_job_with_retries(
    job: JobListing,
    config: AppConfig,
    page: Page,
    context: BrowserContext | None,
    apply_one: ApplyFn,
    *,
    max_attempts: int,
    retry_backoff_ms: int = 1500,
    label: str = "",
) -> bool:
    prefix = f"{label} " if label else ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = await apply_one(page, context, job, config)
        except ApplyBatchStopped:
            raise
        except Exception as exc:
            if is_target_closed_error(exc):
                # The page/context/browser was closed (often the user reopened
                # Chrome mid-run). Retrying is pointless and would crash on the
                # dead page; stop the whole batch instead of flagging this job.
                logger.error(
                    "%sBrowser/page closed during apply for %s — stopping run. "
                    "Do not open Chrome while a run is in progress.",
                    prefix,
                    job.title,
                )
                raise BrowserClosed("browser/page closed during apply") from exc
            if attempt < max_attempts:
                logger.warning(
                    "%sApply error for %s @ %s (attempt %d/%d), retrying...",
                    prefix,
                    job.title,
                    job.company,
                    attempt,
                    max_attempts,
                )
                await asyncio.sleep(retry_backoff_ms / 1000)
                continue
            logger.exception("%sFailed applying to %s after %d attempts", prefix, job.url, max_attempts)
            record_technical_failure(
                config.base_dir,
                job_key=job_key(job.source, job.job_id),
                source=job.source,
                title=job.title,
                company=job.company,
                url=job.url,
                reason=f"exception: {type(exc).__name__}: {str(exc)[:120]}",
            )
            return False

        if result is True:
            record_run_attempt(job_key(job.source, job.job_id))
            return True
        if result is None:
            record_run_attempt(job_key(job.source, job.job_id))
            return None
        if attempt < max_attempts:
            logger.warning(
                "%sApply failed for %s @ %s (attempt %d/%d), retrying...",
                prefix,
                job.title,
                job.company,
                attempt,
                max_attempts,
            )
            await asyncio.sleep(retry_backoff_ms / 1000)
        else:
            logger.warning(
                "%sSkipping %s @ %s after %d failed attempts",
                prefix,
                job.title,
                job.company,
                max_attempts,
            )
    record_run_attempt(job_key(job.source, job.job_id))
    record_technical_failure(
        config.base_dir,
        job_key=job_key(job.source, job.job_id),
        source=job.source,
        title=job.title,
        company=job.company,
        url=job.url,
        reason=f"apply not completed after {max_attempts} attempt(s)",
    )
    return False


async def run_apply_batch(
    jobs: list[JobListing],
    config: AppConfig,
    page: Page,
    context: BrowserContext | None,
    apply_one: ApplyFn,
    *,
    workers: int | None = None,
) -> int:
    """Apply to jobs. Uses parallel tabs when workers > 1. No delay between jobs."""
    if not jobs:
        return 0

    max_attempts = 1 + max(0, config.application.apply_retries)
    backoff_ms = max(0, config.application.retry_backoff_ms)
    workers = max(1, workers if workers is not None else config.application.apply_workers)

    if workers <= 1 or context is None:
        applied = 0
        for job in jobs:
            try:
                result = await _apply_job_with_retries(
                    job,
                    config,
                    page,
                    context,
                    apply_one,
                    max_attempts=max_attempts,
                    retry_backoff_ms=backoff_ms,
                )
                if result is True:
                    applied += 1
            except ApplyBatchStopped as exc:
                logger.warning("%s — stopping apply batch after %d success(es)", exc, applied)
                break
        return applied

    sem = asyncio.Semaphore(workers)
    total = len(jobs)

    async def _one(job: JobListing, index: int) -> bool | None:
        async with sem:
            try:
                tab = await context.new_page()
            except Exception as exc:
                if is_target_closed_error(exc):
                    raise BrowserClosed("context closed before opening tab") from exc
                raise
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
                    retry_backoff_ms=backoff_ms,
                    label=label,
                )
            finally:
                with contextlib.suppress(Exception):
                    await tab.close()

    results = await asyncio.gather(*[_one(job, i) for i, job in enumerate(jobs, 1)], return_exceptions=True)
    applied = sum(1 for ok in results if ok is True)
    if any(isinstance(r, ApplyBatchStopped) for r in results):
        logger.error(
            "Parallel apply stopped early — browser/page was closed. Applied %d before the browser closed.",
            applied,
        )
    else:
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("Apply worker error: %s: %s", type(r).__name__, r)
    logger.info("Parallel apply finished: %d/%d succeeded (%d workers)", applied, total, workers)
    return applied
