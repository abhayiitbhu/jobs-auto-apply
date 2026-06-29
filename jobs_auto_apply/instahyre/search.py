from __future__ import annotations

import hashlib
import logging
import re

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..config import InstahyreFiltersConfig
from ..utils import JobListing
from .auth import INSTAHYRE_OPPORTUNITIES
from .feeds import _wait_for_opportunities, activate_feed, feeds_from_config

logger = logging.getLogger("job_apply")

EMPLOYER_ROW = "div.employer-row"
VIEW_BUTTON = f"{EMPLOYER_ROW} #interested-btn, {EMPLOYER_ROW} button.button-interested"
_VIEW_RE = re.compile(r"view\s*[»>]?", re.I)


async def apply_filters(page: Page, filters: InstahyreFiltersConfig) -> None:
    if filters.search_urls or filters.feeds:
        count = len(filters.feeds) or len(filters.search_urls)
        logger.info("Instahyre using %d configured feed(s)", count)
        return

    await page.goto(INSTAHYRE_OPPORTUNITIES, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)

    for job_fn in filters.job_functions:
        try:
            chip = page.get_by_text(job_fn, exact=True)
            if await chip.count() > 0:
                await chip.first.click()
                await page.wait_for_timeout(350)
        except PlaywrightTimeout:
            logger.warning("Could not select job function: %s", job_fn)

    for location in filters.locations:
        try:
            loc_filter = page.get_by_role("button", name=re.compile("location|where", re.I))
            if await loc_filter.count() > 0:
                await loc_filter.first.click()
                await page.wait_for_timeout(500)
            chip = page.get_by_text(location, exact=False)
            if await chip.count() > 0:
                await chip.first.click()
            await page.keyboard.press("Escape")
        except PlaywrightTimeout:
            logger.warning("Could not filter location: %s", location)

    if filters.experience_years is not None:
        try:
            yoe = page.get_by_text(re.compile(rf"{filters.experience_years}\s*years?", re.I))
            if await yoe.count() > 0:
                await yoe.first.click()
        except PlaywrightTimeout:
            pass

    if filters.company_size and filters.company_size.lower() != "all":
        try:
            size = page.get_by_text(filters.company_size, exact=False)
            if await size.count() > 0:
                await size.first.click()
        except PlaywrightTimeout:
            pass

    await page.wait_for_timeout(1000)
    logger.info("Instahyre filters applied: %s", page.url)


def job_id_from_card(title: str, company: str, feed_url: str = "") -> str:
    raw = f"{feed_url}|{title}|{company}".strip("|")
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def parse_company_title(full_name: str) -> tuple[str, str]:
    """'Knowl - Founding Engineer' -> company Knowl, title Founding Engineer."""
    text = full_name.strip()
    if " - " in text:
        company, title = text.split(" - ", 1)
        return company.strip(), title.strip()
    return text, text


_EXTRACT_JS = """
() => {
  const seen = new Set();
  const rows = [];
  for (const row of document.querySelectorAll("div.employer-row")) {
    const nameEl =
      row.querySelector(".employer-details .employer-job-name .company-name") ||
      row.querySelector(".employer-details .company-name") ||
      row.querySelector(".employer-job-name .company-name");
    const full = (nameEl?.innerText || "").trim();
    if (!full) continue;
    const key = full.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    const parts = full.split(" - ");
    const company = (parts[0] || full).trim();
    const title = (parts.slice(1).join(" - ") || full).trim();
    rows.push({ title, company, full_name: full, card_index: rows.length });
  }
  return rows;
}
"""


def employer_rows(page: Page):
    return page.locator(EMPLOYER_ROW)


def view_buttons(page: Page):
    return page.locator(VIEW_BUTTON)


async def click_view_at(page: Page, index: int) -> bool:
    rows = employer_rows(page)
    if await rows.count() <= index:
        return False
    row = rows.nth(index)
    btn = row.locator("#interested-btn, button.button-interested")
    if await btn.count() > 0:
        try:
            await btn.first.click(timeout=5000)
            return True
        except PlaywrightTimeout:
            pass
    link = row.locator("a#employer-profile-opportunity, a.row.text-link")
    if await link.count() > 0:
        try:
            await link.first.click(timeout=5000)
            return True
        except PlaywrightTimeout:
            pass
    return False


async def _scroll_load_more(page: Page, rounds: int = 15) -> None:
    stable = 0
    last_count = 0
    for _ in range(rounds):
        await page.mouse.wheel(0, 2400)
        await page.wait_for_timeout(700)
        load_more = page.get_by_role("button", name=re.compile(r"load more|show more", re.I))
        if await load_more.count() > 0:
            try:
                await load_more.first.click()
                await page.wait_for_timeout(1000)
            except PlaywrightTimeout:
                pass
        rows = await page.evaluate(_EXTRACT_JS)
        count = len(rows or [])
        if count == last_count:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            last_count = count


async def _merge_jobs_on_page(
    page: Page,
    jobs: dict[str, JobListing],
    limit: int,
    *,
    feed_url: str = "",
) -> int:
    before = len(jobs)
    rows = await page.evaluate(_EXTRACT_JS)
    for row in rows or []:
        if len(jobs) >= limit > 0:
            break
        title = str(row.get("title", "")).strip() or "Unknown"
        company = str(row.get("company", "")).strip()
        card_index = int(row.get("card_index", 0))
        jid = job_id_from_card(title, company, feed_url)
        jobs.setdefault(
            jid,
            JobListing(
                job_id=jid,
                title=title,
                company=company,
                url=feed_url or page.url,
                source="instahyre",
                easy_apply=True,
                meta={"feed_url": feed_url, "card_index": card_index} if feed_url else {"card_index": card_index},
            ),
        )
    return len(jobs) - before


async def collect_job_listings(page: Page, limit: int, *, feed_url: str = "") -> list[JobListing]:
    if not await _wait_for_opportunities(page):
        return []
    await _scroll_load_more(page, rounds=4)
    jobs: dict[str, JobListing] = {}
    await _merge_jobs_on_page(page, jobs, limit if limit > 0 else 10_000, feed_url=feed_url)
    listings = list(jobs.values())
    if limit > 0:
        listings = listings[:limit]
    logger.info("Found %d Instahyre opportunities on page", len(listings))
    return listings


async def collect_from_search_urls(
    page: Page,
    urls: list[str],
    limit: int,
    *,
    feed_dicts: list[dict] | None = None,
    default_job_functions: list[str] | None = None,
    job_function_aliases: dict[str, str] | None = None,
    default_skills: str | None = None,
    skill_chip_values: dict[str, str] | None = None,
    skill_type_queries: dict[str, str] | None = None,
) -> list[JobListing]:
    merged: dict[str, JobListing] = {}
    per_feed = limit if limit > 0 else 5000
    for spec in feeds_from_config(
        search_urls=urls,
        feed_dicts=feed_dicts,
        default_job_functions=default_job_functions,
        job_function_aliases=job_function_aliases,
        default_skills=default_skills,
        skill_chip_values=skill_chip_values,
        skill_type_queries=skill_type_queries,
    ):
        logger.info("Instahyre feed: %s", spec.name)
        feed_key = await activate_feed(page, spec)
        for job in await collect_job_listings(page, per_feed, feed_url=feed_key):
            merged[job.job_id] = job
        if limit > 0 and len(merged) >= limit:
            break
    result = list(merged.values())[:limit] if limit > 0 else list(merged.values())
    logger.info("Found %d Instahyre listings across feeds", len(result))
    return result
