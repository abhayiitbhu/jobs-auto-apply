from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from playwright.async_api import Page

from ..page_load import goto_settled, scroll_lazy_page, wait_for_page_settled

from ..config import HiristFiltersConfig
from ..cookies import slugify
from ..utils import JobListing
from .auth import HIRIST_ORIGIN

logger = logging.getLogger("job_apply")

_JOB_HREF = re.compile(r"/j/[a-z0-9-]+", re.I)


def _paginated_url(base_url: str, page_num: int) -> str:
    """Hirist uses ?page=N (page 1 omits the param)."""
    if page_num <= 1:
        return base_url
    parsed = urlparse(base_url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["page"] = [str(page_num)]
    query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=query))


def _with_experience_params(url: str, filters: HiristFiltersConfig) -> str:
    if filters.experience_min is None and filters.experience_max is None:
        return url
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    if filters.experience_min is not None:
        qs["minexp"] = [str(filters.experience_min)]
    if filters.experience_max is not None:
        qs["maxexp"] = [str(filters.experience_max)]
    query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=query))


def _search_url(filters: HiristFiltersConfig) -> str:
    keyword = slugify(filters.keywords[0] if filters.keywords else "backend")
    if filters.cities:
        city = slugify(filters.cities[0])
        url = f"{HIRIST_ORIGIN}/k/{keyword}-jobs-in-{city}"
    else:
        url = f"{HIRIST_ORIGIN}/k/{keyword}-jobs"
    return _with_experience_params(url, filters)


def feed_urls(filters: HiristFiltersConfig) -> list[str]:
    if filters.search_urls:
        return [_with_experience_params(u, filters) for u in filters.search_urls]
    return [_search_url(filters)]


async def apply_filters(page: Page, filters: HiristFiltersConfig) -> None:
    if filters.search_urls:
        logger.info("Hirist using %d configured search URL(s)", len(filters.search_urls))
        return

    url = _search_url(filters)
    await goto_settled(page, url)

    if filters.experience:
        try:
            exp = page.get_by_text(re.compile(filters.experience.replace("-", r"[-–]"), re.I))
            if await exp.count() > 0:
                await exp.first.click()
                await page.wait_for_timeout(1000)
        except Exception:
            logger.warning("Could not set Hirist experience filter")

    await page.wait_for_timeout(1500)
    logger.info("Hirist filters applied: %s", page.url)


def _job_id(href: str, title: str) -> str:
    return hashlib.sha1(f"{href}|{title}".encode()).hexdigest()[:16]


async def _merge_jobs_on_page(page: Page, jobs: dict[str, JobListing], limit: int) -> int:
    before = len(jobs)
    links = page.locator('a[href*="/j/"]')
    count = await links.count()
    for i in range(count):
        if len(jobs) >= limit:
            break
        link = links.nth(i)
        href = await link.get_attribute("href") or ""
        if not _JOB_HREF.search(href):
            continue
        full_url = urljoin(HIRIST_ORIGIN, href.split("?")[0])
        text = (await link.inner_text()).strip()
        if len(text) < 3:
            continue
        title = text.split("\n")[0].strip()
        if len(title) < 3:
            continue
        job_id = _job_id(full_url, title)
        jobs.setdefault(
            job_id,
            JobListing(
                job_id=job_id,
                title=title,
                company="",
                url=full_url,
                source="hirist",
                easy_apply=True,
            ),
        )
    return len(jobs) - before


async def iter_job_listings(page: Page, limit: int) -> AsyncIterator[JobListing]:
    jobs: dict[str, JobListing] = {}
    await _merge_jobs_on_page(page, jobs, limit)
    for listing in list(jobs.values()):
        yield listing

    stable = 0
    for round_num in range(40):
        if len(jobs) >= limit:
            break
        before = set(jobs.keys())
        await page.mouse.wheel(0, 2500)
        await scroll_lazy_page(page, rounds=2, pause_ms=300)
        await wait_for_page_settled(page, extra_ms=400)
        await _merge_jobs_on_page(page, jobs, limit)
        added = len(jobs) - len(before)
        if added == 0:
            stable += 1
            if stable >= 4:
                break
        else:
            stable = 0
            if round_num % 8 == 7:
                logger.info("Hirist scrolling… %d listings so far", len(jobs))
            for job_id in jobs:
                if job_id not in before:
                    yield jobs[job_id]


async def collect_srp_page(page: Page, limit: int = 500) -> list[JobListing]:
    """Collect jobs on the current SRP page only (no multi-page infinite scroll)."""
    jobs: dict[str, JobListing] = {}
    await _merge_jobs_on_page(page, jobs, limit)
    await scroll_lazy_page(page, rounds=3, pause_ms=250)
    await wait_for_page_settled(page, extra_ms=300)
    await _merge_jobs_on_page(page, jobs, limit)
    return list(jobs.values())


async def iter_paginated_feed_pages(
    page: Page,
    filters: HiristFiltersConfig,
    *,
    max_pages: int | None = None,
) -> AsyncIterator[tuple[str, int, list[JobListing]]]:
    """Yield (feed_url, page_num, jobs) — apply after each page before continuing."""
    pages_cap = max_pages if max_pages is not None else filters.max_pages
    if pages_cap < 1:
        pages_cap = 1

    for feed_url in feed_urls(filters):
        for page_num in range(1, pages_cap + 1):
            url = _paginated_url(feed_url, page_num)
            logger.info("Hirist feed page %d: %s", page_num, url)
            await goto_settled(page, url, timeout_ms=90_000)
            jobs = await collect_srp_page(page)
            if not jobs:
                if page_num > 1:
                    logger.info("Hirist feed ended at page %d (no listings)", page_num)
                break
            yield feed_url, page_num, jobs


async def collect_job_listings(page: Page, limit: int) -> list[JobListing]:
    """Collect all listings on current page with infinite scroll (legacy single-page scrape)."""
    seen: set[str] = set()
    listings: list[JobListing] = []
    async for job in iter_job_listings(page, limit):
        if job.job_id in seen:
            continue
        seen.add(job.job_id)
        listings.append(job)
        if len(listings) >= limit:
            break
    logger.info("Found %d Hirist listings on page", len(listings))
    return listings[:limit]


async def collect_from_search_urls(page: Page, urls: list[str], limit: int) -> list[JobListing]:
    merged: dict[str, JobListing] = {}
    per_feed = limit if limit > 0 else 2000
    for url in urls:
        logger.info("Hirist feed: %s", url)
        await goto_settled(page, url, timeout_ms=90_000)
        for job in await collect_srp_page(page, per_feed):
            merged[job.job_id] = job
        if limit > 0 and len(merged) >= limit:
            break
    result = list(merged.values())[:limit] if limit > 0 else list(merged.values())
    logger.info("Found %d Hirist listings across %d feed(s)", len(result), len(urls))
    return result
