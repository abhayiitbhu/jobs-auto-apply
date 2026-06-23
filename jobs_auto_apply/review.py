from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page

from .cover_letter import build_cover_letter
from .page_load import goto_settled
from .wellfound.company import (
    extract_wellfound_company,
    looks_like_location_not_company,
    parse_company_from_jd,
    repair_cover_letter_company,
)
from .jd import is_noisy_jd
from .salary import is_job_salary_eligible, job_eligibility
from .wellfound.modal import is_apply_metadata_only
from .utils import JobListing, job_key as make_job_key

logger = logging.getLogger("job_apply")


@dataclass
class ReviewItem:
    job_key: str
    source: str
    job_id: str
    title: str
    company: str
    url: str
    status: str = "pending"  # pending | approved | rejected
    cover_letter: str = ""
    jd_excerpt: str = ""
    easy_apply: bool = False
    external_ats: bool = False
    external_url: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_job_listing(self) -> JobListing:
        meta = dict(self.meta)
        if self.cover_letter:
            meta["cover_letter"] = self.cover_letter
        return JobListing(
            job_id=self.job_id,
            title=self.title,
            company=self.company,
            url=self.url,
            source=self.source,
            easy_apply=self.easy_apply,
            external_ats=self.external_ats,
            external_url=self.external_url,
            description=self.jd_excerpt,
            meta=self.meta,
        )

    @classmethod
    def from_job(cls, job: JobListing, *, cover_letter: str = "", jd_excerpt: str = "") -> ReviewItem:
        return cls(
            job_key=make_job_key(job.source, job.job_id),
            source=job.source,
            job_id=job.job_id,
            title=job.title,
            company=job.company,
            url=job.url,
            cover_letter=cover_letter,
            jd_excerpt=jd_excerpt or job.description,
            easy_apply=job.easy_apply,
            external_ats=job.external_ats,
            external_url=job.external_url,
            meta=dict(job.meta),
        )


def review_queue_path(base_dir: Path, platform: str, review_dir: str = "data/review") -> Path:
    return base_dir / review_dir / f"{platform}.json"


def load_review_queue(base_dir: Path, platform: str, review_dir: str = "data/review") -> dict[str, Any]:
    path = review_queue_path(base_dir, platform, review_dir)
    if not path.exists():
        return {"platform": platform, "created_at": None, "items": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_review_queue(
    base_dir: Path, platform: str, payload: dict[str, Any], review_dir: str = "data/review"
) -> Path:
    path = review_queue_path(base_dir, platform, review_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def items_from_payload(payload: dict[str, Any]) -> list[ReviewItem]:
    items: list[ReviewItem] = []
    for raw in payload.get("items", []):
        items.append(ReviewItem(**{k: v for k, v in raw.items() if k in ReviewItem.__dataclass_fields__}))
    return items


async def enrich_job_for_review(
    page: Page, config, job: JobListing, *, with_cover_letter: bool = True
) -> ReviewItem:
    await goto_settled(page, job.url, timeout_ms=60_000)
    old_company = job.company
    min_lpa = config.application.min_inr_salary_lpa

    if job.source == "wellfound":
        from .wellfound.modal import extract_wellfound_job_page

        info = await extract_wellfound_job_page(page, min_inr_lpa=min_lpa)
        jd = info.jd
        modal_text = info.modal_text
        company = await extract_wellfound_company(page, job.title, modal_text or jd)
        if company:
            job.company = company
        elif looks_like_location_not_company(job.company):
            job.company = ""
    else:
        from .jd import extract_job_description

        jd = await extract_job_description(page)
        modal_text = ""

    job.description = jd
    job.meta.update(
        job_eligibility(
            jd=jd,
            meta=job.meta,
            modal=modal_text,
            min_inr_lpa=min_lpa,
        )
    )
    cover = ""
    if with_cover_letter:
        cover = await build_cover_letter(config, job=job, page=None, jd=jd)
        if job.source == "wellfound" and job.company and old_company != job.company:
            cover = repair_cover_letter_company(
                cover, old_company=old_company, new_company=job.company
            )
    return ReviewItem.from_job(job, cover_letter=cover, jd_excerpt=jd)


async def enrich_jobs_parallel(
    context: BrowserContext,
    config,
    jobs: list[JobListing],
    *,
    workers: int | None = None,
) -> list[ReviewItem]:
    """Enrich multiple jobs concurrently — one browser tab per job (shared login)."""
    if not jobs:
        return []

    n_workers = max(1, workers or config.application.enrich_workers)
    sem = asyncio.Semaphore(n_workers)
    total = len(jobs)

    async def _one(job: JobListing, index: int) -> ReviewItem:
        async with sem:
            page = await context.new_page()
            try:
                logger.info("Enriching [%d/%d]: %s @ %s", index, total, job.title, job.company or "?")
                return await enrich_job_for_review(page, config, job, with_cover_letter=True)
            finally:
                await page.close()

    return list(await asyncio.gather(*[_one(job, i) for i, job in enumerate(jobs, 1)]))


async def refresh_cover_letters(config, items: list[ReviewItem]) -> int:
    """Regenerate cover letters from stored JDs (no browser). Returns count updated."""
    updated = 0
    for index, item in enumerate(items, 1):
        if not item.jd_excerpt:
            continue
        logger.info(
            "Cover letter [%d/%d]: %s @ %s",
            index,
            len(items),
            item.title,
            item.company or "?",
        )
        job = item.to_job_listing()
        item.cover_letter = await build_cover_letter(config, job=job, page=None, jd=item.jd_excerpt)
        updated += 1
    return updated


def needs_re_enrich(item: ReviewItem) -> bool:
    return item.source == "wellfound" and (
        not item.jd_excerpt
        or is_noisy_jd(item.jd_excerpt)
        or is_apply_metadata_only(item.jd_excerpt)
        or len(item.jd_excerpt) < 400
    )


def repair_review_item_company(item: ReviewItem) -> bool:
    """Fix company name from stored JD when card parsing picked up location/salary."""
    if item.source != "wellfound":
        return False
    if not looks_like_location_not_company(item.company):
        return False
    old = item.company
    company = parse_company_from_jd(item.title, item.jd_excerpt)
    item.company = company if company else ""
    item.cover_letter = repair_cover_letter_company(
        item.cover_letter, old_company=old, new_company=item.company or "your team"
    )
    return True


def repair_review_queue_items(items: list[ReviewItem]) -> int:
    return sum(1 for item in items if repair_review_item_company(item))


def build_review_payload(platform: str, items: list[ReviewItem]) -> dict[str, Any]:
    return {
        "platform": platform,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "instructions": (
            "Set status to approved or rejected for each item. "
            "Run: python main.py apply-reviewed --platform <name>"
        ),
        "items": [asdict(item) for item in items],
    }


def approved_items(base_dir: Path, platform: str, review_dir: str = "data/review") -> list[ReviewItem]:
    payload = load_review_queue(base_dir, platform, review_dir)
    return [item for item in items_from_payload(payload) if item.status == "approved"]


def review_summary(base_dir: Path, platform: str, review_dir: str = "data/review") -> dict[str, int]:
    items = items_from_payload(load_review_queue(base_dir, platform, review_dir))
    counts = {"pending": 0, "approved": 0, "rejected": 0}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts
