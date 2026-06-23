from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urljoin, urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from ..config import UplersFiltersConfig
from ..cookies import slugify
from ..utils import JobListing
from .auth import UPLERS_ORIGIN, UPLERS_JOBS_URLS

logger = logging.getLogger("job_apply")


async def _open_jobs_page(page: Page) -> None:
    for url in UPLERS_JOBS_URLS:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        if "login" not in page.url.lower() and "joinus" not in page.url.lower():
            return
    await page.goto(UPLERS_JOBS_URLS[0], wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)


async def _type_in_search(page: Page, value: str) -> None:
    search = page.locator(
        'input[type="search"]:visible, input[placeholder*="Search" i]:visible, '
        'input[placeholder*="keyword" i]:visible, input[name*="search" i]:visible'
    )
    if await search.count() == 0:
        return
    await search.first.click()
    await search.first.fill(value)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(2000)


async def _click_filter_chip(page: Page, label: str) -> None:
    chip = page.locator("button, a, span").filter(has_text=re.compile(f"^{re.escape(label)}$", re.I))
    if await chip.count() > 0:
        await chip.first.click()
        await page.wait_for_timeout(800)


async def apply_filters(page: Page, filters: UplersFiltersConfig) -> None:
    await _open_jobs_page(page)

    if filters.keywords:
        await _type_in_search(page, filters.keywords)

    for skill in filters.skills:
        try:
            await _type_in_search(page, skill)
        except PlaywrightTimeout:
            logger.warning("Could not filter by skill: %s", skill)

    for location in filters.locations:
        try:
            filter_btn = page.get_by_role("button", name=re.compile("location|where", re.I))
            if await filter_btn.count() > 0:
                await filter_btn.first.click()
                await page.wait_for_timeout(600)
            await _click_filter_chip(page, location)
            await page.keyboard.press("Escape")
        except PlaywrightTimeout:
            logger.warning("Could not filter by location: %s", location)

    for role in filters.roles:
        try:
            filter_btn = page.get_by_role("button", name=re.compile("role|title", re.I))
            if await filter_btn.count() > 0:
                await filter_btn.first.click()
                await page.wait_for_timeout(600)
            await _click_filter_chip(page, role)
            await page.keyboard.press("Escape")
        except PlaywrightTimeout:
            logger.warning("Could not filter by role: %s", role)

    if filters.remote_only:
        try:
            await _click_filter_chip(page, "Remote")
        except PlaywrightTimeout:
            logger.warning("Could not enable remote-only filter")

    await page.wait_for_timeout(2000)
    logger.info("Uplers filters applied; URL: %s", page.url)


def _make_job_id(href: str, title: str) -> str:
    return hashlib.sha1(f"{href}|{title}".encode()).hexdigest()[:16]


def _parse_job_card_text(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    title = lines[0] if lines else "Unknown"
    company = lines[1] if len(lines) > 1 else ""
    return title, company


async def _scroll_results(page: Page, rounds: int = 6) -> None:
    for _ in range(rounds):
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(1000)


async def collect_job_listings(page: Page, limit: int) -> list[JobListing]:
    await _scroll_results(page)

    listings: list[JobListing] = []
    seen: set[str] = set()

    selectors = [
        'a[href*="/talent/opportunit"]',
        'a[href*="/talent/job"]',
        'a[href*="/opportunit"]',
        '[data-testid*="job"] a',
        ".job-card a",
        "article a",
    ]

    for selector in selectors:
        links = page.locator(selector)
        count = await links.count()
        for i in range(count):
            link = links.nth(i)
            href = await link.get_attribute("href")
            if not href:
                continue
            full_url = urljoin(UPLERS_ORIGIN, href)
            if not _looks_like_job_url(full_url):
                continue
            text = await link.inner_text()
            title, company = _parse_job_card_text(text)
            job_id = _make_job_id(full_url, title)
            if job_id in seen:
                continue
            seen.add(job_id)
            listings.append(
                JobListing(
                    job_id=job_id,
                    title=title,
                    company=company,
                    url=full_url,
                    source="uplers",
                    external_ats=True,
                )
            )
            if len(listings) >= limit:
                break
        if listings:
            break

    # Broader fallback: any apply/view links on page
    if not listings:
        all_links = page.locator("a")
        count = await all_links.count()
        for i in range(min(count, limit * 5)):
            link = all_links.nth(i)
            href = await link.get_attribute("href") or ""
            text = (await link.inner_text()).strip()
            if not href or len(text) < 4:
                continue
            full_url = urljoin(UPLERS_ORIGIN, href)
            if not _looks_like_job_url(full_url):
                continue
            title, company = _parse_job_card_text(text)
            job_id = _make_job_id(full_url, title)
            if job_id in seen:
                continue
            seen.add(job_id)
            listings.append(
                JobListing(
                    job_id=job_id,
                    title=title,
                    company=company or slugify(text)[:40],
                    url=full_url,
                    source="uplers",
                    external_ats=True,
                )
            )
            if len(listings) >= limit:
                break

    logger.info("Found %d Uplers job listings", len(listings))
    return listings[:limit]


def _looks_like_job_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    keywords = ("opportunit", "/job", "/jobs", "/role", "/position", "/opening")
    return any(k in path for k in keywords)
