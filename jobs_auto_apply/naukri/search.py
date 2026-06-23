from __future__ import annotations

import hashlib
import logging
import re
from typing import Any
from urllib.parse import urlencode, urljoin

from playwright.async_api import Page, Response, TimeoutError as PlaywrightTimeout

from ..config import NaukriFiltersConfig
from ..page_load import (
    goto_settled,
    reveal_footer_actions,
    scroll_lazy_page,
    wait_for_page_settled,
)
from ..cookies import slugify
from ..utils import JobListing

logger = logging.getLogger("job_apply")

NAUKRI_ORIGIN = "https://www.naukri.com"

# Naukri TopTier / Aurus SRP (Next.js) — job cards are clickable divs, not <a> tags.
AURUS_CARD_SELECTOR = 'div.cursor-pointer.rounded-3xl.bg-n800:has(.text-title18Sb.text-n100)'

_AURUS_COLLECT_JS = """
() => {
  const cards = [...document.querySelectorAll('div.cursor-pointer.rounded-3xl.bg-n800')]
    .filter((el) => el.querySelector('.text-title18Sb.text-n100'));
  return cards.map((card, index) => {
    const title = card.querySelector('.text-title18Sb.text-n100')?.innerText?.trim() || '';
    const company = card.querySelector('.text-title16Sb.text-n200')?.innerText?.trim() || '';
    const spans = [...card.querySelectorAll('li span.text-n300')].map((s) => s.textContent.trim());
    const experience = spans.find((s) => /\\d+\\s*-\\s*\\d+\\s*yrs?/i.test(s)) || '';
    const location = spans.find((s) => !/yrs?/i.test(s) && !/₹|lac|lakh|disclosed/i.test(s)) || '';
    const salary = spans.find((s) => /₹|lac|lakh|disclosed/i.test(s)) || '';
    const link = card.querySelector('a[href*="job-listings"], a[href*="/job/"], a[href*="-jobs-"]');
    const href = link ? (link.href || link.getAttribute('href') || '') : '';
    const cardText = (card.innerText || '').replace(/\\s+/g, ' ').trim();
    const hasQuickBadge = /quick apply/i.test(cardText)
      || !!card.querySelector('img[src*="naukri-toptier-short"]');
    const externalApply = /apply on company|company site|registered consult|walk-?in only/i.test(cardText);
    let quickApply = null;
    if (hasQuickBadge) quickApply = true;
    else if (externalApply) quickApply = false;
    return { index, title, company, location, experience, salary, href, quickApply };
  }).filter((j) => j.title && j.title.length > 2);
}
"""

_LEGACY_LINK_SELECTORS = (
    'a[href*="-jobs-"]',
    'a[href*="/job-listings-"]',
    'article a.title',
    ".jobTuple a",
    ".srp-jobtuple-wrapper a",
    ".cust-job-tuple a",
)


def _naukri_max_experience(filters: NaukriFiltersConfig) -> int | None:
    """Naukri `experience` param and Aurus slider set max years only (min stays 0)."""
    if filters.experience_max is not None:
        return filters.experience_max
    return filters.experience_min


def _search_url(filters: NaukriFiltersConfig, page: int = 1) -> str:
    keywords = slugify(filters.keywords)
    if filters.locations:
        location = slugify(filters.locations[0])
        path = f"{keywords}-jobs-in-{location}"
    else:
        path = f"{keywords}-jobs"
    if page > 1:
        path = f"{path}-{page}"
    url = f"{NAUKRI_ORIGIN}/{path}"
    params: dict[str, str] = {}
    max_exp = _naukri_max_experience(filters)
    if max_exp is not None:
        params["experience"] = str(max_exp)
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


async def go_to_search_page(page: Page, filters: NaukriFiltersConfig, page_num: int = 1) -> None:
    """Navigate to SRP page N (page 1 applies experience filter on UI when needed)."""
    if hasattr(page, "_naukri_job_url_index"):
        delattr(page, "_naukri_job_url_index")
    ensure_url_index(page)
    url = _search_url(filters, page_num)
    await goto_settled(page, url, scroll=True)
    if page_num == 1:
        max_exp = _naukri_max_experience(filters)
        if max_exp is not None:
            await _apply_experience_filter(page, max_exp)
    await page.wait_for_timeout(1500)
    logger.info("Naukri SRP page %d: %s", page_num, page.url)


def _job_id_from_url(url: str) -> str:
    match = re.search(r"(\d{6,})", url)
    if match:
        return match.group(1)
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _normalize_job_listing_url(path_or_url: str) -> str:
    raw = (path_or_url or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        raw = urljoin(NAUKRI_ORIGIN, raw)
    elif not raw.startswith("http"):
        raw = urljoin(NAUKRI_ORIGIN, raw)
    if "job-listings" not in raw.lower():
        return ""
    return raw.split("?")[0]


class _JobUrlIndex:
    """Maps Aurus SRP cards to /job-listings-... detail URLs."""

    def __init__(self) -> None:
        self.by_job_id: dict[str, str] = {}
        self.by_title: dict[str, str] = {}
        self.urls: list[str] = []
        self.quick_apply_by_job_id: dict[str, bool] = {}

    def add(
        self,
        url: str,
        *,
        title: str = "",
        job_id: str = "",
        quick_apply: bool | None = None,
    ) -> None:
        normalized = _normalize_job_listing_url(url)
        if not normalized:
            return
        if normalized not in self.urls:
            self.urls.append(normalized)
        jid = job_id or _job_id_from_url(normalized)
        if jid:
            self.by_job_id[jid] = normalized
            if quick_apply is not None:
                self.quick_apply_by_job_id[jid] = quick_apply
        if title:
            self.by_title[title.lower().strip()] = normalized

    def quick_apply(self, job_id: str) -> bool | None:
        if not job_id:
            return None
        return self.quick_apply_by_job_id.get(job_id)

    def match(self, title: str, company: str) -> str:
        key = title.lower().strip()
        if key in self.by_title:
            return self.by_title[key]

        title_slug = slugify(title)
        company_slug = slugify(company) if company else ""
        company_token = company_slug.split("-")[0] if company_slug else ""

        for url in self.urls:
            path = url.lower()
            if title_slug and title_slug not in path:
                continue
            if company_token and company_token not in path:
                continue
            return url
        return ""


def _quick_apply_from_api_obj(obj: dict[str, Any]) -> bool | None:
    for key in (
        "quickApply",
        "isQuickApply",
        "quick_apply",
        "isQuickApplyJob",
        "showQuickApply",
    ):
        if key in obj:
            return bool(obj[key])
    for key in (
        "externalApply",
        "isExternalApply",
        "externalRedirect",
        "applyOnCompanySite",
        "isAppliedViaConsultant",
    ):
        if key in obj:
            return not bool(obj[key])
    apply_type = obj.get("applyType") or obj.get("applicationType")
    if isinstance(apply_type, str):
        lowered = apply_type.lower()
        if "quick" in lowered:
            return True
        if any(token in lowered for token in ("external", "company", "walk")):
            return False
    return None


def _resolve_quick_apply(
    card_quick: bool | None,
    url_index: _JobUrlIndex,
    job_id: str,
) -> bool | None:
    if card_quick is True or card_quick is False:
        return bool(card_quick)
    return url_index.quick_apply(job_id)


def _walk_job_json(obj: Any, index: _JobUrlIndex, depth: int = 0) -> None:
    if depth > 24 or obj is None:
        return
    if isinstance(obj, dict):
        title = obj.get("title") or obj.get("jobTitle") or obj.get("designation")
        job_id = str(obj.get("jobId") or obj.get("jobid") or obj.get("jdId") or "")
        url_val = ""
        for key in ("jdURL", "jobUrl", "url", "slug", "jobDetailsUrl", "seoUrl", "jdUrl"):
            val = obj.get(key)
            if isinstance(val, str) and "job-listings" in val:
                url_val = val
                break
        if url_val:
            quick_apply = _quick_apply_from_api_obj(obj)
            index.add(url_val, title=str(title or ""), job_id=job_id, quick_apply=quick_apply)
        for value in obj.values():
            _walk_job_json(value, index, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _walk_job_json(item, index, depth + 1)


async def _capture_search_response(response: Response, index: _JobUrlIndex) -> None:
    if response.request.resource_type not in ("xhr", "fetch"):
        return
    if "naukri.com" not in response.url:
        return
    try:
        if response.status != 200:
            return
        data = await response.json()
    except Exception:
        return
    _walk_job_json(data, index)


async def _extract_listing_urls_from_dom(page: Page) -> list[str]:
    paths = await page.evaluate(
        """
        () => {
          const html = document.documentElement.innerHTML;
          const matches = html.match(/job-listings-[a-z0-9-]+-\\d{6,}/gi) || [];
          return [...new Set(matches)];
        }
        """
    )
    return [_normalize_job_listing_url(path) for path in paths if path]


def _attach_job_url_index(page: Page, index: _JobUrlIndex) -> None:
    async def on_response(response: Response) -> None:
        await _capture_search_response(response, index)

    page.on("response", on_response)


def ensure_url_index(page: Page) -> _JobUrlIndex:
    index = getattr(page, "_naukri_job_url_index", None)
    if index is None:
        index = _JobUrlIndex()
        _attach_job_url_index(page, index)
        page._naukri_job_url_index = index  # type: ignore[attr-defined]
    return index


async def _wait_for_results(page: Page) -> bool:
    """Wait for either the new Aurus SRP or legacy job tuples to render."""
    try:
        await page.wait_for_selector(
            '#jobs-list-header, header:has-text("jobs for you"), .srp-jobtuple-wrapper, .jobTuple',
            timeout=20_000,
        )
    except PlaywrightTimeout:
        pass

    try:
        await page.wait_for_selector(AURUS_CARD_SELECTOR, timeout=8_000)
        return True
    except PlaywrightTimeout:
        pass

    for sel in _LEGACY_LINK_SELECTORS:
        if await page.locator(sel).count() > 0:
            return True
    return False


async def _scroll_results(page: Page, rounds: int = 3) -> None:
    for round_num in range(rounds):
        await scroll_lazy_page(page, rounds=3, pause_ms=350)
        await reveal_footer_actions(page)
        load_more = page.locator("#load-more-btn, button:has-text('Load more'), [class*='load-more']")
        if await load_more.count() > 0:
            try:
                btn = load_more.first
                await btn.scroll_into_view_if_needed()
                await btn.click(timeout=3000)
                await wait_for_page_settled(page, extra_ms=800)
            except PlaywrightTimeout:
                pass


async def _collect_aurus_listings(
    page: Page,
    limit: int,
    url_index: _JobUrlIndex,
    *,
    quick_apply_only: bool = True,
) -> list[JobListing]:
    raw = await page.evaluate(_AURUS_COLLECT_JS)
    if not raw:
        return []

    logger.info("Aurus SRP: found %d job cards on page", len(raw))
    listings: list[JobListing] = []
    seen: set[str] = set()
    with_url = 0
    skipped_non_quick = 0
    missing: list[str] = []

    for item in raw:
        if len(listings) >= limit:
            break

        title = (item.get("title") or "").strip()
        company = (item.get("company") or "").strip()
        experience = (item.get("experience") or "").strip()
        location = (item.get("location") or "").strip()
        salary = (item.get("salary") or "").strip()
        href = (item.get("href") or "").strip()
        card_quick = item.get("quickApply")

        url = _normalize_job_listing_url(href)
        if not url:
            url = url_index.match(title, company)

        if not url:
            missing.append(title)
            continue

        job_id = _job_id_from_url(url)
        if job_id in seen:
            continue

        quick_apply = _resolve_quick_apply(card_quick, url_index, job_id)
        if quick_apply_only and quick_apply is not True:
            if quick_apply is False:
                skipped_non_quick += 1
            continue

        seen.add(job_id)
        with_url += 1

        listings.append(
            JobListing(
                job_id=job_id,
                title=title,
                company=company,
                url=url,
                source="naukri",
                easy_apply=quick_apply is not False,
                meta={
                    "location": location,
                    "salary": salary,
                    "experience": experience,
                    "quick_apply": quick_apply is True,
                },
            )
        )

    if missing:
        logger.warning(
            "Aurus SRP: could not resolve job-listings URL for %d cards (e.g. %s)",
            len(missing),
            ", ".join(missing[:3]),
        )
    if quick_apply_only and skipped_non_quick:
        logger.info(
            "Aurus SRP: skipped %d non-quick-apply cards",
            skipped_non_quick,
        )
    logger.info(
        "Aurus SRP: resolved %d / %d quick-apply job-listings URLs",
        with_url,
        len(raw),
    )
    return listings


async def _collect_legacy_listings(
    page: Page,
    limit: int,
    *,
    quick_apply_only: bool = True,
) -> list[JobListing]:
    listings: list[JobListing] = []
    seen: set[str] = set()
    skipped_non_quick = 0

    for selector in _LEGACY_LINK_SELECTORS:
        anchors = page.locator(selector)
        count = await anchors.count()
        for i in range(count):
            if len(listings) >= limit:
                break
            link = anchors.nth(i)
            href = await link.get_attribute("href")
            if not href:
                continue
            url = urljoin(NAUKRI_ORIGIN, href) if href.startswith("/") else href
            job_id = _job_id_from_url(url)
            if job_id in seen:
                continue

            title = (await link.inner_text()).strip()
            if not title or len(title) < 3:
                continue

            parent = link.locator("xpath=ancestor::*[contains(@class,'tuple') or contains(@class,'job')][1]")
            company = ""
            parent_text = ""
            if await parent.count() > 0:
                comp = parent.locator(".comp-name, .companyInfo, .subTitle, .company")
                if await comp.count() > 0:
                    company = (await comp.first.inner_text()).strip()
                parent_text = (await parent.first.inner_text()).lower()

            quick_apply = "quick apply" in parent_text
            external_apply = any(
                token in parent_text
                for token in ("company site", "apply on company", "registered consult")
            )
            if external_apply:
                quick_apply = False
            elif not quick_apply:
                quick_apply = None

            if quick_apply_only and quick_apply is not True:
                if quick_apply is False:
                    skipped_non_quick += 1
                continue

            seen.add(job_id)

            listings.append(
                JobListing(
                    job_id=job_id,
                    title=title,
                    company=company,
                    url=url,
                    source="naukri",
                    easy_apply=quick_apply is not False,
                    meta={"quick_apply": quick_apply is True},
                )
            )
        if listings:
            break

    if quick_apply_only and skipped_non_quick:
        logger.info("Legacy SRP: skipped %d non-quick-apply listings", skipped_non_quick)

    return listings


_SLIDER_MAX_YEARS = 15


async def _drag_slider_handle(
    page: Page,
    handle,
    track_box: dict[str, float],
    years: int,
) -> None:
    box = await handle.bounding_box()
    if not box or not track_box or track_box["width"] <= 0:
        return
    fraction = min(max(years / _SLIDER_MAX_YEARS, 0.0), 1.0)
    start_x = box["x"] + box["width"] / 2
    start_y = box["y"] + box["height"] / 2
    target_x = track_box["x"] + track_box["width"] * fraction
    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    await page.mouse.move(target_x, start_y, steps=12)
    await page.mouse.up()
    await page.wait_for_timeout(400)


async def _apply_experience_filter(page: Page, max_years: int) -> None:
    """Set Naukri max experience (Aurus slider is 0–N yrs; URL ?experience=N is also max)."""
    track = page.locator(
        ".Experience_sliderContainer___ZY_i .rc-slider, .exp-container .rc-slider"
    ).first
    handles = page.locator(
        ".Experience_sliderContainer___ZY_i .rc-slider-handle, "
        ".Experience_sliderContainer___ZY_i .handle, "
        ".exp-container .rc-slider-handle, "
        ".exp-container .handle"
    )
    if await track.count() > 0 and await handles.count() > 0:
        try:
            track_box = await track.bounding_box()
            handle_count = await handles.count()
            if track_box and track_box["width"] > 0:
                # Range UI: left handle = 0 (min), right handle = max — only move max.
                handle = handles.nth(1) if handle_count >= 2 else handles.first
                await _drag_slider_handle(page, handle, track_box, max_years)
                await page.wait_for_timeout(1500)
                logger.info("Set Naukri max experience to %d yrs (0–%d)", max_years, max_years)
                return
        except PlaywrightTimeout:
            pass

    for label in (f"{max_years} years", f"{max_years} Yrs", f"{max_years} yrs"):
        chip = page.get_by_text(re.compile(rf"^{re.escape(label)}$", re.I))
        if await chip.count() > 0:
            try:
                await chip.first.click(timeout=3000)
                await page.wait_for_timeout(1500)
                logger.info("Set Naukri experience filter: %s", label)
                return
            except PlaywrightTimeout:
                pass

    logger.warning("Could not set Naukri experience filter")


async def apply_filters(page: Page, filters: NaukriFiltersConfig) -> None:
    ensure_url_index(page)
    await go_to_search_page(page, filters, page_num=1)
    logger.info("Naukri filters applied: %s", page.url)


async def collect_job_listings(
    page: Page,
    limit: int,
    *,
    quick_apply_only: bool = True,
) -> list[JobListing]:
    all_listings: list[JobListing] = []
    seen: set[str] = set()
    url_index = ensure_url_index(page)

    if not await _wait_for_results(page):
        logger.warning("Naukri results did not load in time")
        return []

    scroll_rounds = 6 if quick_apply_only and limit > 0 else 3
    await _scroll_results(page, rounds=scroll_rounds)

    for url in await _extract_listing_urls_from_dom(page):
        url_index.add(url)

    logger.info("Aurus SRP: indexed %d job-listings URLs from page/API", len(url_index.urls))

    collect_limit = limit * 4 if quick_apply_only and limit > 0 else limit
    aurus = await _collect_aurus_listings(
        page,
        collect_limit,
        url_index,
        quick_apply_only=quick_apply_only,
    )
    if aurus:
        for job in aurus:
            if job.job_id not in seen:
                seen.add(job.job_id)
                all_listings.append(job)
    else:
        legacy = await _collect_legacy_listings(
            page,
            collect_limit,
            quick_apply_only=quick_apply_only,
        )
        for job in legacy:
            if job.job_id not in seen:
                seen.add(job.job_id)
                all_listings.append(job)

    if quick_apply_only:
        logger.info("Found %d Naukri quick-apply listings", len(all_listings))
    else:
        logger.info("Found %d Naukri listings", len(all_listings))
    return all_listings[:limit]


async def collect_srp_page(
    page: Page,
    limit: int,
    *,
    quick_apply_only: bool = True,
) -> list[JobListing]:
    """Collect jobs on the current Naukri SRP page only."""
    return await collect_job_listings(page, limit, quick_apply_only=quick_apply_only)
