from __future__ import annotations

import logging

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


async def retry_pending_jobs(config: AppConfig, job_refs: list[PendingJobRef]) -> int:
    """Re-open skipped jobs and apply now that pending answers are saved."""
    if config.application.dry_run:
        logger.info("Dry run — skipping live retry for %d job(s)", len(job_refs))
        return 0

    from .hirist.apply import apply_to_job as hirist_apply_to_job
    from .naukri.apply import apply_to_job as naukri_apply_to_job

    applied_ids = load_applied_jobs(config.applied_jobs_path)
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
        async with naukri_session(config) as (_, context, page):
            for ref in naukri_refs:
                job = job_listing_from_ref(ref)
                try:
                    if await naukri_apply_to_job(page, job, config):
                        total += 1
                except Exception:
                    logger.exception("Live retry failed for Naukri: %s", ref.url)

    hirist_refs = by_source.get("hirist", [])
    if hirist_refs:
        async with hirist_session(config) as (_, context, page):
            for ref in hirist_refs:
                job = job_listing_from_ref(ref)
                try:
                    result = await hirist_apply_to_job(page, context, job, config)
                    if result is True:
                        total += 1
                except Exception:
                    logger.exception("Live retry failed for Hirist: %s", ref.url)

    return total
