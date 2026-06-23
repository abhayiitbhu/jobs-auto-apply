from __future__ import annotations

from dataclasses import dataclass

from .utils import JobListing


@dataclass(frozen=True)
class PendingJobRef:
    source: str
    title: str
    company: str
    url: str


def job_listing_from_ref(ref: PendingJobRef) -> JobListing:
    slug = ref.url.rstrip("/").split("/")[-1] or ref.url
    return JobListing(
        job_id=slug,
        title=ref.title or slug,
        company=ref.company,
        url=ref.url,
        source=ref.source,
        easy_apply=True,
    )
