from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Error as PlaywrightError

from .browser import hirist_session, naukri_session
from .config import AppConfig
from .pending_job_ref import PendingJobRef, job_listing_from_ref
from .utils import job_key, load_applied_jobs

logger = logging.getLogger("job_apply")


def _dedupe_jobs(refs: list[PendingJobRef]) -> list[PendingJobRef]:
    seen: set[str] = set()
    out: list[PendingJobRef] = []
    for ref in refs:
        url = ref.url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(ref)
    return out


async def _apply_with_session_retries(
    config: AppConfig,
    *,
    platform_label: str,
    session_factory,
    apply_batch,
    jobs,
) -> int:
    """Open a browser session with retries (Chrome profile may need time to release)."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            async with session_factory(config) as (_, context, page):
                return await apply_batch(page, context, jobs, config)
        except (PlaywrightError, RuntimeError, OSError) as exc:
            last_exc = exc
            logger.warning(
                "%s retry session failed (attempt %d/3): %s",
                platform_label,
                attempt + 1,
                exc,
            )
            if attempt < 2:
                await asyncio.sleep(2.5 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


async def retry_pending_jobs(config: AppConfig, job_refs: list[PendingJobRef]) -> int:
    """Re-open skipped jobs and apply now that pending answers are saved."""
    if config.application.dry_run:
        logger.info("Dry run — skipping live retry for %d job(s)", len(job_refs))
        return 0

    from .hirist.apply import apply_batch as hirist_apply_batch
    from .naukri.apply import apply_batch as naukri_apply_batch

    applied_ids = load_applied_jobs(config.applied_jobs_path, include_deferred=False)
    by_source: dict[str, list[PendingJobRef]] = {}
    for ref in _dedupe_jobs(job_refs):
        key = job_key(ref.source, job_listing_from_ref(ref).job_id)
        if key in applied_ids:
            logger.debug("Already applied, skip retry: %s", ref.url)
            continue
        by_source.setdefault(ref.source, []).append(ref)

    total = 0
    naukri_refs = by_source.get("naukri", [])
    if naukri_refs:
        jobs = [job_listing_from_ref(ref) for ref in naukri_refs]
        total += await _apply_with_session_retries(
            config,
            platform_label="Naukri",
            session_factory=naukri_session,
            apply_batch=naukri_apply_batch,
            jobs=jobs,
        )

    hirist_refs = by_source.get("hirist", [])
    if hirist_refs:
        jobs = [job_listing_from_ref(ref) for ref in hirist_refs]
        total += await _apply_with_session_retries(
            config,
            platform_label="Hirist",
            session_factory=hirist_session,
            apply_batch=hirist_apply_batch,
            jobs=jobs,
        )

    return total
