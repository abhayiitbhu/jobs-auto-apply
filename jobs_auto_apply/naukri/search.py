from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from typing import Any
from urllib.parse import urlencode, urljoin

from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..config import NaukriFiltersConfig
from ..cookies import slugify
from ..page_load import (
    goto_settled,
    reveal_footer_actions,
    scroll_lazy_page,
    wait_for_page_settled,
)
from ..utils import JobListing

logger = logging.getLogger("job_apply")

NAUKRI_ORIGIN = "https://www.naukri.com"

# Naukri TopTier / Aurus SRP (Next.js) — job cards are clickable divs, not <a> tags.
AURUS_CARD_SELECTOR = "div.cursor-pointer.rounded-3xl.bg-n800:has(.text-title18Sb.text-n100)"

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
    // Branded / "Hot vacancy" cards are clickable <div>s that navigate via JS
    // instead of an <a href>. Recover the SEO job-listings URL from anywhere in
    // the card's own markup (data attrs, embedded JSON) so it maps to THIS card
    // without relying on fuzzy title/company matching.
    let cardUrl = '';
    const urlHit = card.innerHTML.match(/job-listings-[a-z0-9-]+-\\d{6,}/i);
    if (urlHit) cardUrl = urlHit[0];
    const cardText = (card.innerText || '').replace(/\\s+/g, ' ').trim();
    const postedMatch = cardText.match(
      /(?:\\bposted\\b\\s*)?(\\d+\\s*(?:days?|hours?)\\s*ago|few\\s*hours?\\s*ago|just\\s*now|today|yesterday)/i
    );
    const posted = postedMatch ? postedMatch[0].trim() : '';
    const externalApply = /apply on company|company site|registered consult|walk-?in only/i.test(cardText);
    const hasQuickBadgeEl = !!(
      card.querySelector('[class*="quickApply"], [class*="quick-apply"], [class*="QuickApply"]')
      || card.querySelector('img[alt*="Quick"], img[alt*="quick"]')
    );
    const hasQuickText = /\\bquick\\s*apply\\b/i.test(cardText)
      && !/\\b(not|no|without)\\b[^\\n]{0,24}\\bquick\\s*apply\\b/i.test(cardText);
    let quickApply = null;
    if (externalApply) quickApply = false;
    else if (hasQuickBadgeEl || hasQuickText) quickApply = true;
    let jobId = '';
    const idEl = card.querySelector('[data-job-id], [data-jid], [data-jobid]');
    if (idEl) {
      jobId = idEl.getAttribute('data-job-id') || idEl.getAttribute('data-jid') || idEl.getAttribute('data-jobid') || '';
    }
    if (!jobId) {
      const hit = card.innerHTML.match(/(?:job[Ii]d|jd[Ii]d|jobid)["'\\s:=]+["']?(\\d{6,})/);
      if (hit) jobId = hit[1];
    }
    return { index, title, company, location, experience, salary, href, cardUrl, quickApply, posted, jobId };
  }).filter((j) => j.title && j.title.length > 2);
}
"""

_LEGACY_LINK_SELECTORS = (
    'a[href*="-jobs-"]',
    'a[href*="/job-listings-"]',
    "article a.title",
    ".jobTuple a",
    ".srp-jobtuple-wrapper a",
    ".cust-job-tuple a",
)


def _naukri_max_experience(filters: NaukriFiltersConfig) -> int | None:
    """Naukri `experience` param and Aurus slider set max years only (min stays 0)."""
    if filters.experience_max is not None:
        return filters.experience_max
    return filters.experience_min


_NAUKRI_JOB_AGE_BUCKETS = (1, 3, 7, 15, 30)


def _naukri_job_age_label(days: int) -> str:
    if days == 1:
        return "Last 1 day"
    return f"Last {days} days"


def _naukri_job_age_bucket(max_days: int) -> int:
    """Smallest Naukri Freshness bucket that covers max_days (for sidebar filter)."""
    for bucket in _NAUKRI_JOB_AGE_BUCKETS:
        if bucket >= max_days:
            return bucket
    return _NAUKRI_JOB_AGE_BUCKETS[-1]


def _job_age_input_id(label: str) -> str:
    return f"select-{label}-jobAge"


_JOB_AGE_FILTER_JS = """
(label) => {
  const jobAgeInput = document.querySelector('[id$="-jobAge"]');
  if (jobAgeInput) {
    const section = jobAgeInput.closest('.relative.box-border');
    if (section && !section.classList.contains('isOpen')) {
      const toggle = section.querySelector('.cursor-pointer');
      if (toggle) toggle.click();
    }
  }

  const inputId = `select-${label}-jobAge`;
  const input = document.getElementById(inputId);
  if (!input) return { found: false };
  const checked = input.checked || input.getAttribute('aria-checked') === 'true';
  if (checked) return { found: true, already: true };
  const labelEl = document.querySelector(`label[for="${inputId}"]`);
  if (labelEl) {
    labelEl.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    labelEl.click();
    return { found: true, clicked: 'label' };
  }
  input.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  input.click();
  return { found: true, clicked: 'input' };
}
"""

_SORT_NEWEST_VALUES = frozenset({"date", "newest", "freshness"})

_ORDER_BY_TRIGGER = re.compile(r"order\s+by|sort\s+by", re.I)
_ALREADY_FRESHNESS = re.compile(r"order\s+by\s+freshness|sorted\s+by\s+freshness", re.I)

_FRESHNESS_SORT_RADIO = "#select-Freshness-sort"
_FRESHNESS_SORT_LABEL = 'label[for="select-Freshness-sort"]'

_FRESHNESS_SORT_JS = """
() => {
  const headers = [...document.querySelectorAll('.text-title16Sb.text-n100')];
  const sortHeader = headers.find((el) => el.textContent.trim() === 'Sort by');
  if (sortHeader) {
    const section = sortHeader.closest('.relative.box-border');
    if (section && !section.classList.contains('isOpen')) {
      const toggle = sortHeader.closest('.cursor-pointer');
      if (toggle) toggle.click();
    }
  }

  const input = document.querySelector('#select-Freshness-sort');
  if (!input) return { found: false };
  const checked = input.checked || input.getAttribute('aria-checked') === 'true';
  if (checked) return { found: true, already: true };
  const label = document.querySelector('label[for="select-Freshness-sort"]');
  if (label) {
    label.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    label.click();
    return { found: true, clicked: 'label' };
  }
  input.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  input.click();
  return { found: true, clicked: 'input' };
}
"""


def _naukri_sort_param(sort: str) -> str | None:
    if sort.lower() in _SORT_NEWEST_VALUES:
        return "date"
    return None


def _posted_days_ago(text: str) -> float:
    """Lower = newer. Unknown dates sort last."""
    if not text:
        return 9999.0
    lowered = text.lower().strip()
    if any(token in lowered for token in ("just now", "few hours", "hour ago", "hours ago")):
        return 0.0
    if "today" in lowered:
        return 0.0
    if "yesterday" in lowered:
        return 1.0
    match = re.search(r"(\d+)\s*days?\s*ago", lowered)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+)\s*hours?\s*ago", lowered)
    if match:
        return float(match.group(1)) / 24.0
    return 9999.0


def _sort_jobs_by_posted(jobs: list[JobListing], sort: str) -> list[JobListing]:
    if _naukri_sort_param(sort) is None:
        return jobs
    return sorted(jobs, key=lambda job: _posted_days_ago(str(job.meta.get("posted", ""))))


def _job_within_max_age(job: JobListing, max_days: int) -> bool:
    posted = str(job.meta.get("posted", "")).strip()
    if not posted:
        return True
    return _posted_days_ago(posted) <= float(max_days)


def _filter_jobs_by_max_age(jobs: list[JobListing], max_days: int | None) -> list[JobListing]:
    if max_days is None or max_days <= 0:
        return jobs
    kept = [job for job in jobs if _job_within_max_age(job, max_days)]
    skipped = len(jobs) - len(kept)
    if skipped:
        logger.info("Filtered %d Naukri jobs older than %d days", skipped, max_days)
    return kept


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
    sort_param = _naukri_sort_param(filters.sort)
    if sort_param:
        params["sortBy"] = sort_param
    # Bake the freshness/age filter into the URL so the very first SRP render is
    # already filtered, instead of loading unfiltered and re-rendering after a
    # sidebar radio click. Naukri accepts ?jobAge=N (1/3/7/15/30 days).
    if filters.max_job_age_days is not None and filters.max_job_age_days > 0:
        params["jobAge"] = str(_naukri_job_age_bucket(filters.max_job_age_days))
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


async def _freshness_sort_already_selected(page: Page) -> bool:
    radio = page.locator(_FRESHNESS_SORT_RADIO)
    if await radio.count() > 0:
        try:
            if await radio.is_checked():
                return True
        except Exception:
            pass
        aria = await radio.get_attribute("aria-checked")
        if aria and aria.lower() == "true":
            return True

    header = page.locator("#jobs-list-header, header").first
    if await header.count() > 0:
        header_text = (await header.inner_text()).strip()
        if _ALREADY_FRESHNESS.search(header_text):
            return True
    return False


async def _click_sidebar_freshness_sort(page: Page) -> bool:
    """Aurus left filter panel: Sort by → Freshness radio (not the job-age Freshness section)."""
    result = await page.evaluate(_FRESHNESS_SORT_JS)
    if result.get("found"):
        if result.get("already"):
            return True
        await page.wait_for_timeout(600)
        await _wait_for_results(page)
        logger.info("Set Naukri sort order: Freshness (sidebar radio)")
        return True

    label = page.locator(_FRESHNESS_SORT_LABEL)
    if await label.count() > 0:
        await label.first.scroll_into_view_if_needed()
        await label.first.click(timeout=3000)
        await page.wait_for_timeout(600)
        await _wait_for_results(page)
        logger.info("Set Naukri sort order: Freshness (sidebar label)")
        return True

    radio = page.locator(_FRESHNESS_SORT_RADIO)
    if await radio.count() > 0:
        await radio.first.scroll_into_view_if_needed()
        await radio.first.click(timeout=3000)
        await page.wait_for_timeout(600)
        await _wait_for_results(page)
        logger.info("Set Naukri sort order: Freshness (sidebar input)")
        return True
    return False


async def _click_dropdown_freshness_sort(page: Page) -> bool:
    """Fallback for SRP variants that expose Order by / Sort by as a header dropdown."""
    triggers = page.locator("button, [role='button']").filter(has_text=_ORDER_BY_TRIGGER)
    if await triggers.count() == 0:
        triggers = page.locator('[class*="sort" i] button, [aria-label*="sort" i]')
    if await triggers.count() == 0:
        triggers = page.get_by_role("button", name=_ORDER_BY_TRIGGER)
    if await triggers.count() == 0:
        return False

    trigger = triggers.first
    trigger_text = (await trigger.inner_text()).strip()
    if _ALREADY_FRESHNESS.search(trigger_text):
        return True

    await trigger.click(timeout=3000)
    await page.wait_for_timeout(600)

    for pattern in (
        re.compile(r"^freshness$", re.I),
        re.compile(r"^date$", re.I),
        re.compile(r"newest|posted\s*date|most\s*recent", re.I),
    ):
        opt = page.get_by_role("option", name=pattern)
        if await opt.count() == 0:
            opt = page.get_by_role("menuitem", name=pattern)
        if await opt.count() == 0:
            opt = page.locator("li, button, a, span, div").filter(has_text=pattern)
        if await opt.count() > 0:
            await opt.first.click(timeout=3000)
            await page.wait_for_timeout(1000)
            logger.info("Set Naukri sort order: Freshness (dropdown)")
            return True

    await page.keyboard.press("Escape")
    return False


async def _apply_sort_filter(page: Page, sort: str) -> None:
    """Set Aurus SRP sort to Freshness (Naukri's label for newest postings first)."""
    if _naukri_sort_param(sort) is None:
        return
    try:
        if await _freshness_sort_already_selected(page):
            logger.info("Naukri sort already: Freshness")
            return

        if await _click_sidebar_freshness_sort(page):
            return
        if await _click_dropdown_freshness_sort(page):
            return

        logger.debug("Naukri sort control not found; using URL sortBy=%s", _naukri_sort_param(sort))
    except PlaywrightTimeout:
        logger.debug("Naukri sort UI not available; using URL sortBy=%s", _naukri_sort_param(sort))


async def _job_age_filter_already_selected(page: Page, label: str) -> bool:
    radio = page.locator(f'[id="{_job_age_input_id(label)}"]')
    if await radio.count() == 0:
        return False
    try:
        if await radio.is_checked():
            return True
    except Exception:
        pass
    aria = await radio.get_attribute("aria-checked")
    return bool(aria and aria.lower() == "true")


async def _apply_job_age_filter(page: Page, max_days: int | None) -> None:
    """Set Naukri Freshness filter (job posting age), e.g. Last 7 days."""
    if max_days is None or max_days <= 0:
        return
    bucket = _naukri_job_age_bucket(max_days)
    label = _naukri_job_age_label(bucket)
    try:
        if await _job_age_filter_already_selected(page, label):
            logger.info("Naukri job age filter already: %s", label)
            return

        result = await page.evaluate(_JOB_AGE_FILTER_JS, label)
        if result.get("found"):
            if result.get("already"):
                logger.info("Naukri job age filter already: %s", label)
                return
            await page.wait_for_timeout(600)
            await _wait_for_results(page)
            logger.info(
                "Set Naukri job age filter: %s (max_job_age_days=%d)",
                label,
                max_days,
            )
            return

        input_id = _job_age_input_id(label)
        option = page.locator(f'label[for="{input_id}"]')
        if await option.count() > 0:
            await option.first.scroll_into_view_if_needed()
            await option.first.click(timeout=3000)
            await page.wait_for_timeout(600)
            await _wait_for_results(page)
            logger.info(
                "Set Naukri job age filter: %s (max_job_age_days=%d)",
                label,
                max_days,
            )
            return

        logger.warning("Could not set Naukri job age filter: %s", label)
    except PlaywrightTimeout:
        logger.warning("Naukri job age filter UI timed out for %s", label)


async def go_to_search_page(page: Page, filters: NaukriFiltersConfig) -> None:
    """Navigate to a fully-filtered Naukri SRP (Aurus uses infinite scroll, not URL pages).

    All filters — keywords, location, experience, sort and job age — are baked
    into the search URL so the very first results render is already filtered. The
    sidebar steps below are now only verification fallbacks: each early-returns
    when the URL already applied the filter, so they no-op in the common case
    instead of clicking a control and forcing an extra unfiltered re-render.
    """
    if hasattr(page, "_naukri_job_url_index"):
        delattr(page, "_naukri_job_url_index")
    ensure_url_index(page)
    url = _search_url(filters, page=1)
    await goto_settled(page, url, scroll=True)
    max_exp = _naukri_max_experience(filters)
    if max_exp is not None:
        await _apply_experience_filter(page, max_exp)
    await _apply_job_age_filter(page, filters.max_job_age_days)
    await _apply_sort_filter(page, filters.sort)
    await page.wait_for_timeout(1000)
    logger.info("Naukri SRP ready: %s", page.url)


def _job_id_from_url(url: str) -> str:
    match = re.search(r"(\d{6,})", url)
    if match:
        return match.group(1)
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _normalize_job_listing_url(path_or_url: str) -> str:
    raw = (path_or_url or "").strip()
    if not raw:
        return ""
    if raw.startswith("/") or not raw.startswith("http"):
        raw = urljoin(NAUKRI_ORIGIN, raw)
    if "job-listings" not in raw.lower():
        return ""
    return raw.split("?")[0]


def _url_matches_company(url: str, company: str) -> bool:
    """Reject fuzzy URL matches that point at a different employer."""
    company_slug = slugify(company)
    if not company_slug:
        return True
    path = url.lower()
    if company_slug in path:
        return True
    tokens = [t for t in company_slug.split("-") if len(t) > 2]
    if not tokens:
        tokens = [company_slug.split("-")[0]]
    return any(token in path for token in tokens)


# Same job posted across many cities yields one /job-listings- URL per city,
# differing only by the city slug — used to disambiguate masked-company cards
# that share a title.
_CITY_ALIASES = {
    "bangalore": "bengaluru",
    "gurgaon": "gurugram",
    "delhi-ncr": "delhi",
    "new-delhi": "delhi",
}

_LOCATION_NOISE = frozenset({"hybrid", "remote", "work-from-office", "wfo", "wfh", "onsite"})


def _location_tokens(location: str) -> list[str]:
    """City slugs from a card's location string (handles 'Hybrid - Pune, Bengaluru')."""
    tokens: list[str] = []
    for part in re.split(r"[,/|]|\s-\s|\sand\s", location):
        slug = slugify(part)
        if not slug or slug in _LOCATION_NOISE:
            continue
        slug = _CITY_ALIASES.get(slug, slug)
        if slug not in tokens:
            tokens.append(slug)
    return tokens


def _disambiguate_by_location(candidates: list[str], location: str) -> list[str]:
    """Narrow same-title candidate URLs to the one(s) whose city slug matches the card."""
    tokens = _location_tokens(location)
    if not tokens:
        return candidates
    matched = [url for url in candidates if any(tok in url.lower() for tok in tokens)]
    return matched or candidates


class _JobUrlIndex:
    """Maps Aurus SRP cards to /job-listings-... detail URLs."""

    def __init__(self) -> None:
        self.by_job_id: dict[str, str] = {}
        self.by_title_company: dict[str, str] = {}
        self.urls: list[str] = []
        self.quick_apply_by_job_id: dict[str, bool] = {}

    def _title_company_key(self, title: str, company: str) -> str:
        return f"{title.lower().strip()}|{slugify(company)}"

    def add(
        self,
        url: str,
        *,
        title: str = "",
        company: str = "",
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
        if title and company:
            self.by_title_company[self._title_company_key(title, company)] = normalized

    def quick_apply(self, job_id: str) -> bool | None:
        if not job_id:
            return None
        return self.quick_apply_by_job_id.get(job_id)

    def url_for_job_id(self, job_id: str) -> str:
        if not job_id:
            return ""
        return self.by_job_id.get(job_id, "")

    def match(self, title: str, company: str) -> str:
        title = title.strip()
        company = company.strip()
        if not title or not company:
            return ""

        composite = self._title_company_key(title, company)
        if composite in self.by_title_company:
            return self.by_title_company[composite]

        title_slug = slugify(title)
        company_slug = slugify(company)
        if not title_slug or not company_slug:
            return ""

        company_tokens = [t for t in company_slug.split("-") if len(t) > 2]
        if not company_tokens:
            company_tokens = [company_slug.split("-")[0]]

        best_url = ""
        best_score = 0
        for url in self.urls:
            path = url.lower()
            if title_slug not in path:
                continue
            score = 0
            if company_slug in path:
                score += 10
            for token in company_tokens:
                if token in path:
                    score += 3
            if score > best_score:
                best_score = score
                best_url = url

        if best_score >= 3:
            return best_url
        return ""

    def candidates_by_title(self, title: str) -> list[str]:
        """All distinct captured URLs whose path contains this card's title slug."""
        title_slug = slugify(title)
        if not title_slug:
            return []
        out: list[str] = []
        for url in self.urls:
            if title_slug in url.lower() and url not in out:
                out.append(url)
        return out

    def match_by_title_location(self, title: str, location: str) -> str:
        """Resolve masked-company cards: title slug + city, only if unambiguous.

        Naukri hides the employer on many branded cards ("Hiring for an IT
        Services & Consulting company"), defeating the company-aware match. The
        title slug is highly specific; when it maps to several cities, the card's
        rendered location picks the right one. Returns "" when still ambiguous so
        we never guess a wrong employer's URL.
        """
        candidates = self.candidates_by_title(title)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            narrowed = _disambiguate_by_location(candidates, location)
            if len(narrowed) == 1:
                return narrowed[0]
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
        if key in obj and bool(obj[key]):
            return False
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
    """Combine Aurus card badge with quickApply flags captured from SRP API responses."""
    api = url_index.quick_apply(job_id) if job_id else None
    if api is False:
        return False
    if card_quick is False:
        return False
    if api is True:
        return True
    if card_quick is True:
        return True
    return None


def _walk_job_json(obj: Any, index: _JobUrlIndex, depth: int = 0) -> None:
    if depth > 24 or obj is None:
        return
    if isinstance(obj, dict):
        title = obj.get("title") or obj.get("jobTitle") or obj.get("designation")
        company = obj.get("companyName") or obj.get("company") or obj.get("companyNameEncoded") or ""
        job_id = str(obj.get("jobId") or obj.get("jobid") or obj.get("jdId") or "")
        url_val = ""
        for key in ("jdURL", "jobUrl", "url", "slug", "jobDetailsUrl", "seoUrl", "jdUrl"):
            val = obj.get(key)
            if isinstance(val, str) and "job-listings" in val:
                url_val = val
                break
        if url_val:
            quick_apply = _quick_apply_from_api_obj(obj)
            index.add(
                url_val,
                title=str(title or ""),
                company=str(company or ""),
                job_id=job_id,
                quick_apply=quick_apply,
            )
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
    with contextlib.suppress(PlaywrightTimeout):
        await page.wait_for_selector(
            '#jobs-list-header, header:has-text("jobs for you"), .srp-jobtuple-wrapper, .jobTuple',
            timeout=20_000,
        )

    try:
        await page.wait_for_selector(AURUS_CARD_SELECTOR, timeout=8_000)
        return True
    except PlaywrightTimeout:
        pass

    for sel in _LEGACY_LINK_SELECTORS:
        if await page.locator(sel).count() > 0:
            return True
    return False


_AURUS_CARD_COUNT_JS = "() => document.querySelectorAll('div.cursor-pointer.rounded-3xl.bg-n800').length"


async def _scroll_results(page: Page, rounds: int = 3, *, min_cards: int = 0) -> None:
    for round_num in range(rounds):
        if min_cards > 0:
            try:
                count = int(await page.evaluate(_AURUS_CARD_COUNT_JS))
                if count >= min_cards:
                    break
            except Exception:
                pass
        await scroll_lazy_page(page, rounds=2, pause_ms=250)
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
    kept_unknown = 0
    skipped_api_reject = 0
    missing: list[str] = []

    for item in raw:
        if len(listings) >= limit:
            break

        title = (item.get("title") or "").strip()
        company = (item.get("company") or "").strip()
        experience = (item.get("experience") or "").strip()
        location = (item.get("location") or "").strip()
        salary = (item.get("salary") or "").strip()
        posted = (item.get("posted") or "").strip()
        href = (item.get("href") or "").strip()
        card_url = (item.get("cardUrl") or "").strip()
        card_job_id = str(item.get("jobId") or "").strip()
        card_quick = item.get("quickApply")

        # A URL taken straight from this card's markup (href or recovered
        # cardUrl) belongs to this card — no fuzzy matching, so don't second
        # guess it with the company-name heuristic below.
        url = _normalize_job_listing_url(href) or _normalize_job_listing_url(card_url)
        # URLs we trust enough to skip the company-name guard below: taken from
        # the card's own markup, keyed by an exact jobId, or uniquely resolved by
        # title+city (where the company is masked and so can't be checked anyway).
        url_trusted = bool(url)
        if not url and card_job_id:
            url = url_index.url_for_job_id(card_job_id)
            url_trusted = bool(url)
        if not url:
            url = url_index.match(title, company)
        # Masked-company branded cards have no href/jobId and the company name is
        # hidden, so the fuzzy match above fails. Fall back to the title slug
        # disambiguated by the card's city (skips genuinely ambiguous collisions).
        if not url:
            url = url_index.match_by_title_location(title, location)
            url_trusted = bool(url)

        if not url:
            missing.append(title)
            continue

        if not company and not href and not card_url and not card_job_id:
            missing.append(title)
            continue

        job_id = card_job_id or _job_id_from_url(url)
        if not url_trusted and company and not _url_matches_company(url, company):
            logger.debug(
                "Aurus SRP: URL does not match company for %s @ %s — skipping",
                title,
                company,
            )
            continue
        if job_id in seen:
            continue

        api_quick = url_index.quick_apply(job_id) if job_id else None
        quick_apply = _resolve_quick_apply(card_quick, url_index, job_id)
        # Only drop cards that are CLEARLY external/non-quick-apply. Cards with an
        # unknown status (no badge on the SRP card, no API flag) are kept — Naukri
        # SRP often omits the quick-apply badge, so dropping them loses real
        # quick-apply jobs. The detail page (_naukri_detail_is_non_quick_apply)
        # is the source of truth and skips any external job that slips through.
        if quick_apply_only and quick_apply is False:
            skipped_non_quick += 1
            if card_quick is True and api_quick is False:
                skipped_api_reject += 1
            continue
        if quick_apply is None:
            kept_unknown += 1

        seen.add(job_id)
        with_url += 1

        listings.append(
            JobListing(
                job_id=job_id,
                title=title,
                company=company,
                url=url,
                source="naukri",
                easy_apply=True,
                meta={
                    "location": location,
                    "salary": salary,
                    "experience": experience,
                    "quick_apply": quick_apply if quick_apply is not None else "unknown",
                    "posted": posted,
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
            "Aurus SRP: skipped %d non-quick-apply cards%s",
            skipped_non_quick,
            f" ({skipped_api_reject} badge/API mismatch)" if skipped_api_reject else "",
        )
    if quick_apply_only and kept_unknown:
        logger.info(
            "Aurus SRP: kept %d cards with unknown quick-apply status (detail page will verify)",
            kept_unknown,
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
                token in parent_text for token in ("company site", "apply on company", "registered consult")
            )
            if external_apply:
                quick_apply = False
            elif not quick_apply:
                quick_apply = None

            # Keep unknown-status listings; only drop clearly external ones.
            # The detail page is the source of truth for quick-apply eligibility.
            if quick_apply_only and quick_apply is False:
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
                    easy_apply=True,
                    meta={"quick_apply": quick_apply if quick_apply is not None else "unknown"},
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


async def _experience_filter_already_applied(page: Page, max_years: int) -> bool:
    """True when the Aurus slider's max handle already sits at ``max_years``.

    The search URL carries ``?experience=N`` so the SRP usually renders with the
    slider pre-set. Detecting that lets us skip the redundant drag (and the extra
    results re-render it would trigger) — the URL is the source of truth.
    """
    handles = page.locator(
        ".Experience_sliderContainer___ZY_i .rc-slider-handle, "
        ".Experience_sliderContainer___ZY_i .handle, "
        ".exp-container .rc-slider-handle, "
        ".exp-container .handle"
    )
    count = await handles.count()
    if count == 0:
        return False
    handle = handles.nth(1) if count >= 2 else handles.first
    for attr in ("aria-valuenow", "aria-valuetext"):
        try:
            value = await handle.get_attribute(attr)
        except Exception:
            value = None
        if value:
            match = re.search(r"\d+", value)
            if match and int(match.group()) == max_years:
                return True
    return False


async def _apply_experience_filter(page: Page, max_years: int) -> None:
    """Set Naukri max experience (Aurus slider is 0-N yrs; URL ?experience=N is also max)."""
    if await _experience_filter_already_applied(page, max_years):
        logger.info("Naukri experience filter already: 0–%d yrs (from URL)", max_years)
        return
    track = page.locator(".Experience_sliderContainer___ZY_i .rc-slider, .exp-container .rc-slider").first
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
    await go_to_search_page(page, filters)
    logger.info("Naukri filters applied: %s", page.url)


_SCROLL_LAST_CARD_JS = """
() => {
  const cards = [...document.querySelectorAll('div.cursor-pointer.rounded-3xl.bg-n800')]
    .filter((el) => el.querySelector('.text-title18Sb.text-n100'));
  if (cards.length) {
    cards[cards.length - 1].scrollIntoView({ block: 'end', behavior: 'instant' });
  }
  window.scrollBy(0, Math.max(400, window.innerHeight * 0.85));
}
"""


async def _aurus_card_count(page: Page) -> int:
    try:
        return int(await page.evaluate(_AURUS_CARD_COUNT_JS))
    except Exception:
        return 0


async def scroll_naukri_srp_more(page: Page) -> bool:
    """Scroll the infinite-scroll SRP to load more job cards. Returns True if count grew."""
    before = await _aurus_card_count(page)
    for _ in range(5):
        with contextlib.suppress(Exception):
            await page.evaluate(_SCROLL_LAST_CARD_JS)
        await page.wait_for_timeout(350)
    load_more = page.locator("#load-more-btn, button:has-text('Load more'), [class*='load-more']")
    if await load_more.count() > 0:
        try:
            btn = load_more.first
            await btn.scroll_into_view_if_needed()
            await btn.click(timeout=3000)
            await wait_for_page_settled(page, extra_ms=500)
        except PlaywrightTimeout:
            pass
    after = await _aurus_card_count(page)
    if after > before:
        logger.info("Naukri SRP scroll: %d → %d job cards visible", before, after)
    return after > before


async def collect_naukri_srp_batch(
    page: Page,
    limit: int,
    *,
    seen_job_ids: set[str],
    quick_apply_only: bool = True,
    sort: str = "freshness",
    max_job_age_days: int | None = None,
    initial_scroll: bool = False,
) -> list[JobListing]:
    """Collect quick-apply jobs visible on SRP that were not in seen_job_ids."""
    url_index = ensure_url_index(page)
    if not await _wait_for_results(page):
        logger.warning("Naukri results did not load in time")
        return []

    if initial_scroll:
        min_cards = min(limit * 2, 400) if limit > 0 else 0
        await _scroll_results(page, rounds=24, min_cards=min_cards)

    for url in await _extract_listing_urls_from_dom(page):
        url_index.add(url)

    collect_limit = limit * 4 if quick_apply_only and limit > 0 else limit
    all_listings: list[JobListing] = []
    seen: set[str] = set()
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

    sorted_listings = _sort_jobs_by_posted(all_listings, sort)
    filtered = _filter_jobs_by_max_age(sorted_listings, max_job_age_days)
    new_jobs = [j for j in filtered if j.job_id not in seen_job_ids]
    if quick_apply_only:
        logger.info(
            "Found %d new Naukri quick-apply listing(s) (%d visible, %d already seen)",
            len(new_jobs),
            len(filtered),
            len(filtered) - len(new_jobs),
        )
    else:
        logger.info("Found %d new Naukri listing(s)", len(new_jobs))
    cap = limit if limit > 0 else len(new_jobs)
    return new_jobs[:cap]


async def collect_job_listings(
    page: Page,
    limit: int,
    *,
    quick_apply_only: bool = True,
    sort: str = "freshness",
    max_job_age_days: int | None = None,
) -> list[JobListing]:
    all_listings: list[JobListing] = []
    seen: set[str] = set()
    url_index = ensure_url_index(page)

    if not await _wait_for_results(page):
        logger.warning("Naukri results did not load in time")
        return []

    min_cards = min(limit * 2, 400) if limit > 0 else 0
    scroll_rounds = 24 if quick_apply_only and limit > 0 else 2
    await _scroll_results(page, rounds=scroll_rounds, min_cards=min_cards)

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
    sorted_listings = _sort_jobs_by_posted(all_listings, sort)
    if _naukri_sort_param(sort) and sorted_listings != all_listings:
        logger.info("Sorted Naukri listings by date posted (newest first)")
    filtered = _filter_jobs_by_max_age(sorted_listings, max_job_age_days)
    return filtered[:limit]


async def collect_srp_page(
    page: Page,
    limit: int,
    *,
    quick_apply_only: bool = True,
    sort: str = "freshness",
    max_job_age_days: int | None = None,
) -> list[JobListing]:
    """Collect jobs on the current Naukri SRP page only."""
    return await collect_job_listings(
        page,
        limit,
        quick_apply_only=quick_apply_only,
        sort=sort,
        max_job_age_days=max_job_age_days,
    )
