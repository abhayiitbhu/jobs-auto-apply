from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..config import WellfoundFiltersConfig
from ..page_load import goto_settled
from ..utils import JobListing
from .card import meta_from_card_text, pick_job_title_from_card

logger = logging.getLogger("job_apply")

RECENTLY_ACTIVE_LABELS = {
    "day": "Within last 24 hours",
    "week": "Within last week",
    "month": "Within last month",
}

REMOTE_POLICY_LABELS = {
    "none": "None",
    "some": "Some",
    "only": "Only remote",
}


async def _click_filter_button(page: Page, label: str) -> None:
    if page.is_closed():
        raise PlaywrightError("Page closed")
    button = page.get_by_role("button", name=re.compile(rf"^{re.escape(label)}$", re.I))
    if await button.count() == 0:
        button = page.locator("button").filter(has_text=re.compile(label, re.I))
    if await button.count() == 0:
        raise PlaywrightTimeout(f"No filter button: {label}")
    await button.first.click(timeout=8000)
    await page.wait_for_timeout(800)


NAV_JOB_SLUGS = frozenset({"home", "applications", "messages", "profile", "discover", "search", "saved", "settings"})


def is_wellfound_job_url(url: str) -> bool:
    if re.search(r"/company/[^/]+/jobs/", url):
        return True
    match = re.search(r"/jobs/([^/?#]+)", url)
    if not match:
        return False
    slug = match.group(1).lower()
    if slug in NAV_JOB_SLUGS:
        return False
    return "-" in slug and len(slug) > 8


def _job_id_from_url(url: str) -> str:
    company_match = re.search(r"/company/([^/]+)/jobs/([^/?#]+)", url)
    if company_match:
        return f"{company_match.group(1)}-{company_match.group(2)}"
    match = re.search(r"/jobs/([^/?#]+)", url)
    return match.group(1) if match else url


async def _open_profile_job_feed(page: Page) -> None:
    """Open the job feed directly, relying on filters already saved on the profile."""
    await goto_settled(page, "https://wellfound.com/jobs")
    logger.info("Opened Wellfound jobs feed at %s", page.url)


async def _select_checkbox_option(page: Page, option: str) -> None:
    checkbox = page.get_by_role("checkbox", name=re.compile(re.escape(option), re.I))
    if await checkbox.count() > 0:
        if not await checkbox.first.is_checked():
            await checkbox.first.click()
        return
    option_el = page.get_by_role("option", name=re.compile(re.escape(option), re.I))
    if await option_el.count() > 0:
        await option_el.first.click()
        return
    item = page.locator("label, li, div").filter(has_text=re.compile(f"^{re.escape(option)}$", re.I))
    await item.first.click(timeout=5000)


async def _type_autocomplete(page: Page, value: str) -> None:
    control = page.locator(".select__control").last
    if await control.count() > 0:
        await control.click(timeout=5000)
    else:
        textbox = page.locator(
            'input[type="text"]:visible, input[type="search"]:visible, [role="combobox"]:visible input'
        ).last
        await textbox.click(timeout=5000)
    await page.keyboard.type(value, delay=60)
    await page.wait_for_timeout(1200)
    option = page.get_by_role("option", name=re.compile(re.escape(value), re.I))
    if await option.count() > 0:
        await option.first.click()
    else:
        await page.keyboard.press("Enter")


async def _apply_ui_filters(page: Page, filters: WellfoundFiltersConfig) -> None:
    """Best-effort UI filters; failures are logged and skipped."""
    for role in filters.roles[:1]:
        try:
            await _click_filter_button(page, "Role")
            await _type_autocomplete(page, role)
            await page.keyboard.press("Escape")
        except (PlaywrightTimeout, PlaywrightError) as exc:
            logger.warning("Could not set role filter %s: %s", role, exc)

    for location in filters.locations[:2]:
        try:
            await _click_filter_button(page, "Location")
            await _type_autocomplete(page, location)
            await page.keyboard.press("Escape")
        except (PlaywrightTimeout, PlaywrightError) as exc:
            logger.warning("Could not set location filter %s: %s", location, exc)

    if filters.remote_policy in REMOTE_POLICY_LABELS:
        try:
            await _click_filter_button(page, "Location")
            remote_btn = page.get_by_text(REMOTE_POLICY_LABELS[filters.remote_policy], exact=True)
            if await remote_btn.count() > 0:
                await remote_btn.first.click()
            await page.keyboard.press("Escape")
        except (PlaywrightTimeout, PlaywrightError) as exc:
            logger.warning("Could not set remote policy: %s", exc)

    if filters.keywords:
        try:
            keywords_input = page.get_by_placeholder(re.compile("keyword", re.I))
            if await keywords_input.count() == 0:
                keywords_input = page.locator('input[name*="keyword" i], input[placeholder*="Search" i]')
            if await keywords_input.count() > 0:
                await keywords_input.first.fill(filters.keywords)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(1500)
        except (PlaywrightTimeout, PlaywrightError) as exc:
            logger.warning("Could not set keywords in UI: %s", exc)

    for skill in filters.skills[:3]:
        try:
            await _click_filter_button(page, "Skills")
            await _type_autocomplete(page, skill)
            await page.keyboard.press("Escape")
        except (PlaywrightTimeout, PlaywrightError) as exc:
            logger.warning("Could not set skill %s: %s", skill, exc)

    for level in filters.experience_levels[:1]:
        try:
            await _click_filter_button(page, "Experience")
            await _select_checkbox_option(page, level)
            await page.keyboard.press("Escape")
        except (PlaywrightTimeout, PlaywrightError) as exc:
            logger.warning("Could not set experience level %s: %s", level, exc)

    for job_type in filters.job_types[:1]:
        try:
            await _click_filter_button(page, "Job type")
            await _select_checkbox_option(page, job_type)
            await page.keyboard.press("Escape")
        except (PlaywrightTimeout, PlaywrightError) as exc:
            logger.warning("Could not set job type %s: %s", job_type, exc)

    if filters.recently_active and filters.recently_active in RECENTLY_ACTIVE_LABELS:
        try:
            await _click_filter_button(page, "Last active")
            await _select_checkbox_option(page, RECENTLY_ACTIVE_LABELS[filters.recently_active])
            await page.keyboard.press("Escape")
        except (PlaywrightTimeout, PlaywrightError) as exc:
            logger.warning("Could not set recency filter: %s", exc)

    if filters.sort == "newest":
        try:
            sort_btn = page.get_by_role("button", name=re.compile("Sort", re.I))
            if await sort_btn.count() > 0:
                await sort_btn.first.click()
                await page.get_by_text("Newest", exact=True).first.click()
        except (PlaywrightTimeout, PlaywrightError) as exc:
            logger.warning("Could not set sort order: %s", exc)


async def apply_filters(page: Page, filters: WellfoundFiltersConfig) -> None:
    if filters.use_profile_filters:
        await _open_profile_job_feed(page)
        await page.wait_for_timeout(2000)
        logger.info("Using saved Wellfound profile filters (no config overrides)")
        return

    await goto_settled(page, "https://wellfound.com/jobs")
    await _apply_ui_filters(page, filters)
    await page.wait_for_timeout(2500)
    logger.info("Filters applied; current URL: %s", page.url)


def _flatten_apollo_node(node: Any, cache: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "__ref" in node and len(node) == 1:
            ref = node["__ref"]
            return _flatten_apollo_node(cache.get(ref, node), cache)
        return {k: _flatten_apollo_node(v, cache) for k, v in node.items()}
    if isinstance(node, list):
        return [_flatten_apollo_node(item, cache) for item in node]
    return node


def _extract_jobs_from_next_data(page_text: str) -> list[JobListing]:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_text, re.S)
    if not match:
        return []

    payload = json.loads(match.group(1))
    apollo = payload.get("props", {}).get("pageProps", {}).get("apolloState", {})
    cache: dict[str, Any] = apollo.get("data", apollo) if isinstance(apollo, dict) else {}
    if not isinstance(cache, dict):
        return []

    listings: list[JobListing] = []
    seen: set[str] = set()

    for key, value in cache.items():
        if not isinstance(value, dict):
            continue
        typename = value.get("__typename", "")
        if typename not in {"JobListing", "StartupJobListing", "JobListingSearchResult"}:
            continue

        job_id = str(value.get("id") or value.get("slug") or key.split(":")[-1])
        if job_id in seen:
            continue
        seen.add(job_id)

        title = str(value.get("title") or value.get("primaryRoleTitle") or "Unknown role")
        company = ""
        company_ref = value.get("startup") or value.get("company")
        if isinstance(company_ref, dict):
            company = str(company_ref.get("name") or company_ref.get("companyName") or "")
        elif isinstance(company_ref, str) and company_ref in cache:
            company = str(cache[company_ref].get("name", ""))

        slug = value.get("slug") or value.get("jobListingSlug")
        startup_slug = value.get("startupSlug") or value.get("companySlug")
        if slug and startup_slug:
            url = f"https://wellfound.com/jobs/{startup_slug}-{slug}"
        else:
            url = str(value.get("url") or value.get("jobUrl") or f"https://wellfound.com/jobs/{job_id}")

        if not is_wellfound_job_url(url):
            continue

        easy_apply = bool(value.get("isEasyApply") or value.get("easyApply"))
        external_ats = bool(value.get("externalApplicationUrl") or value.get("atsUrl"))

        listings.append(
            JobListing(
                job_id=job_id,
                title=title,
                company=company or "Unknown company",
                url=url,
                source="wellfound",
                easy_apply=easy_apply,
                external_ats=external_ats,
                external_url=str(value.get("externalApplicationUrl") or value.get("atsUrl") or "") or None,
            )
        )

    return listings


async def _scroll_results(page: Page, rounds: int = 6) -> None:
    for _ in range(rounds):
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(800)


async def _click_load_more(page: Page) -> bool:
    for label in (r"load more", r"show more", r"see more"):
        btn = page.get_by_role("button", name=re.compile(label, re.I))
        if await btn.count() > 0:
            try:
                await btn.first.click(timeout=2500)
                await page.wait_for_timeout(1200)
                return True
            except PlaywrightTimeout:
                pass
    return False


async def _merge_jobs_on_page(page: Page, jobs: dict[str, JobListing], limit: int) -> int:
    """Add newly discovered jobs from __NEXT_DATA__ and DOM. Returns count added."""
    before = len(jobs)

    for listing in _extract_jobs_from_next_data(await page.content()):
        if len(jobs) >= limit:
            break
        jobs.setdefault(listing.job_id, listing)

    seen = set(jobs.keys())
    anchors = page.locator('a[href*="/jobs/"], a[href*="/company/"][href*="/jobs/"]')
    count = await anchors.count()
    for i in range(count):
        if len(jobs) >= limit:
            break
        href = await anchors.nth(i).get_attribute("href")
        if not href:
            continue
        full_url = f"https://wellfound.com{href}" if href.startswith("/") else href
        if not is_wellfound_job_url(full_url):
            continue
        job_id = _job_id_from_url(full_url)
        if job_id in seen:
            continue
        text = (await anchors.nth(i).inner_text()).strip()
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        title = pick_job_title_from_card(lines) or job_id
        seen.add(job_id)
        jobs[job_id] = JobListing(
            job_id=job_id,
            title=title,
            company="",
            url=full_url,
            source="wellfound",
            easy_apply="easy apply" in text.lower(),
            external_ats=False,
            meta=meta_from_card_text(text),
        )

    cards = page.locator('[data-test="JobCard"], [data-test="StartupResult"]')
    card_count = await cards.count()
    for i in range(card_count):
        if len(jobs) >= limit:
            break
        card = cards.nth(i)
        link = card.locator('a[href*="/jobs/"], a[href*="/company/"][href*="/jobs/"]').first
        if await link.count() == 0:
            continue
        href = await link.get_attribute("href")
        if not href:
            continue
        full_url = f"https://wellfound.com{href}" if href.startswith("/") else href
        if not is_wellfound_job_url(full_url):
            continue
        job_id = _job_id_from_url(full_url)
        if job_id in seen:
            continue
        text = await card.inner_text()
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        title = pick_job_title_from_card(lines) or job_id
        seen.add(job_id)
        jobs[job_id] = JobListing(
            job_id=job_id,
            title=title or job_id,
            company="",
            url=full_url,
            source="wellfound",
            easy_apply="easy apply" in text.lower(),
            external_ats=False,
            meta=meta_from_card_text(text),
        )

    return len(jobs) - before


async def collect_job_listings(page: Page, limit: int) -> list[JobListing]:
    """Scroll the Wellfound feed until listings stop growing (up to limit)."""
    jobs: dict[str, JobListing] = {}
    await _merge_jobs_on_page(page, jobs, limit)

    max_scroll_rounds = 150 if limit > 100 else 30
    stable_rounds = 0
    for round_num in range(max_scroll_rounds):
        if len(jobs) >= limit:
            break
        len(jobs)
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(700)
        if round_num % 4 == 3:
            await _click_load_more(page)
        added = await _merge_jobs_on_page(page, jobs, limit)
        if added == 0:
            stable_rounds += 1
            if stable_rounds >= 6:
                break
        else:
            stable_rounds = 0
            if round_num % 10 == 9:
                logger.info("Scrolling… collected %d listings so far", len(jobs))

    result = list(jobs.values())[:limit]
    logger.info("Found %d job listings (limit %d)", len(result), limit)
    return result


async def iter_job_listings(page: Page, limit: int) -> AsyncIterator[JobListing]:
    """Yield new listings as the feed is scrolled (for streaming apply pipeline)."""
    jobs: dict[str, JobListing] = {}
    await _merge_jobs_on_page(page, jobs, limit)
    for listing in list(jobs.values()):
        yield listing

    max_scroll_rounds = 150 if limit > 100 else 30
    stable_rounds = 0
    for round_num in range(max_scroll_rounds):
        if len(jobs) >= limit:
            break
        await page.mouse.wheel(0, 3000)
        await page.wait_for_timeout(700)
        if round_num % 4 == 3:
            await _click_load_more(page)
        before_keys = set(jobs.keys())
        await _merge_jobs_on_page(page, jobs, limit)
        added = len(jobs) - len(before_keys)
        if added == 0:
            stable_rounds += 1
            if stable_rounds >= 6:
                break
        else:
            stable_rounds = 0
            if round_num % 10 == 9:
                logger.info("Scrolling… %d listings discovered", len(jobs))
            new_ids = [jid for jid in jobs if jid not in before_keys]
            for job_id in new_ids:
                yield jobs[job_id]

    logger.info("Feed scroll complete — %d listings discovered", len(jobs))
