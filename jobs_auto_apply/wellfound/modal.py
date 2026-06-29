from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..jd import clean_jd_text, is_noisy_jd
from ..page_load import prepare_interactive_page
from ..salary import combined_salary_text, eligibility_summary
from .job_data import WellfoundJobData, parse_job_detail

logger = logging.getLogger("job_apply")

APPLY_BUTTON = re.compile(r"^(Easy Apply|Apply|Apply now)$", re.I)

JD_START = re.compile(
    r"(About (?:Us|the (?:Role|Job|Company|Position)|The (?:Role|Opportunity|Company))|"
    r"Requirements|Responsibilities|What you(?:'ll| will)|Job [Dd]escription|"
    r"The Role|The Opportunity|About the job|Must Haves|Why You|"
    r"Who you are|What we're looking|Key [Rr]esponsibilities|Qualifications|"
    r"Role [Oo]verview|What you'll do)",
    re.I,
)

WELLFOUND_PAGE_JD_SELECTORS = (
    '[data-test="JobDescription"]',
    '[data-test="job-description"]',
    '[data-test="JobDescriptionSection"]',
    '[class*="styles_description"]',
    'div[class*="description__"]',
    '[data-testid*="job-description" i]',
    '[class*="JobDescription" i]',
    '[class*="jobDescription" i]',
    '[class*="job-description" i]',
    '[class*="styles_jobDescription" i]',
    "article",
    "section",
)

PAGE_TAIL = re.compile(
    r"\n(?:Similar jobs|Apply now|YOUR APPLICATION|Refer a friend)\b",
    re.I,
)

PARA_ROOT_SELECTORS = (
    '[data-test="JobDescription"]',
    '[data-test="job-description"]',
    '[class*="styles_description"]',
    'div[class*="description__"]',
    '[data-testid*="job-description" i]',
    '[class*="JobDescription" i]',
    '[class*="jobDescription" i]',
    '[class*="job-description" i]',
    '[class*="styles_jobDescription" i]',
    "#root",
    "body",
)

_PARA_NOISE = re.compile(
    r"^(apply to|remote only|in office|full time|visa sponsorship|not available|"
    r"relocation|hires remotely|company location|job type|"
    r"preferred timezones|collaboration hours|no equity|see more|sign in|similar jobs)$",
    re.I,
)

_JD_BODY_MARKERS = re.compile(
    r"Core Responsibilities|Required Experience|Key Skills|Prefer to Have|"
    r"engineering team is solving|What you|Responsibilities|Qualifications|"
    r"About the (?:Role|Company|job)|The role is|"
    r"Description\s*:|About\s+\w+\s*:|Employment Type|What you'll do",
    re.I,
)


@dataclass
class WellfoundApplyModal:
    modal_text: str = ""
    jd: str = ""
    eligibility: dict = field(default_factory=dict)
    opened: bool = False
    # Populated from the page's __NEXT_DATA__ Apollo cache when available.
    company: str = ""
    company_about: str = ""
    salary_display: str = ""
    skills: list = field(default_factory=list)


async def click_apply(page: Page) -> bool:
    await prepare_interactive_page(page, fast=False)
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=APPLY_BUTTON)
        if await loc.count() > 0:
            try:
                await loc.first.scroll_into_view_if_needed(timeout=5000)
                await loc.first.click(timeout=8000)
                await page.wait_for_timeout(2500)
                return True
            except (PlaywrightTimeout, PlaywrightError):
                continue
    return False


async def close_apply_modal(page: Page) -> None:
    for sel in (
        page.get_by_role("button", name=re.compile(r"close|cancel|dismiss|x", re.I)),
        page.locator('[aria-label*="close" i]'),
    ):
        if await sel.count() > 0:
            try:
                await sel.first.click(timeout=3000)
                await page.wait_for_timeout(800)
                return
            except (PlaywrightTimeout, PlaywrightError):
                continue
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(800)
    except PlaywrightError:
        pass


async def _modal_container(page: Page):
    dialog = page.locator('[role="dialog"]')
    if await dialog.count() > 0:
        return dialog.last
    modal = page.locator('[class*="modal" i], [class*="Modal" i]')
    if await modal.count() > 0:
        return modal.last
    return None


async def _scroll_container(container) -> None:
    with contextlib.suppress(PlaywrightError):
        await container.evaluate(
            """el => {
                const nodes = [el, ...el.querySelectorAll('*')];
                for (const node of nodes) {
                    const style = window.getComputedStyle(node);
                    if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 40) {
                        let last = -1;
                        for (let i = 0; i < 24; i++) {
                            node.scrollTop = node.scrollHeight;
                            if (node.scrollTop === last) break;
                            last = node.scrollTop;
                        }
                    }
                }
            }"""
        )


def is_apply_metadata_only(text: str) -> bool:
    """Apply modal sidebar: title, salary, skills — not the role description body."""
    if not text or len(text) < 80:
        return True
    t = text.strip()
    if not t.startswith("APPLY TO"):
        return False
    body = JD_START.search(t)
    if body and body.start() > 80:
        return False
    return not (len(t) > 1800 and t.count("\n\n") >= 2)


def _strip_page_chrome(text: str) -> str:
    m = JD_START.search(text)
    if m and m.start() > 0:
        text = text[m.start() :]
    tail = PAGE_TAIL.search(text)
    if tail:
        text = text[: tail.start()]
    return clean_jd_text(text)


async def _scroll_job_page(page: Page) -> None:
    with contextlib.suppress(PlaywrightError):
        await page.evaluate(
            """async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                let last = -1;
                for (let i = 0; i < 18; i++) {
                    window.scrollBy(0, Math.max(400, window.innerHeight * 0.85));
                    await delay(250);
                    if (window.scrollY === last) break;
                    last = window.scrollY;
                }
                window.scrollTo(0, 0);
            }"""
        )
    await page.wait_for_timeout(600)


def _is_jd_paragraph(text: str) -> bool:
    t = text.strip()
    if len(t) < 15 or t.startswith("APPLY TO"):
        return False
    if len(t) < 30 and _PARA_NOISE.match(t):
        return False
    return not (len(t) < 20 and re.match(r"^(experience|skills|relocation|visa)$", t, re.I))


def _join_jd_paragraphs(parts: list[str]) -> str:
    if not parts:
        return ""
    jd = clean_jd_text("\n\n".join(parts))
    tail = PAGE_TAIL.search(jd)
    if tail:
        jd = jd[: tail.start()].strip()
    return jd


def _finalize_page_jd(text: str) -> str:
    if not text:
        return ""
    text = clean_jd_text(text)
    tail = PAGE_TAIL.search(text)
    if tail:
        text = text[: tail.start()].strip()
    if is_apply_metadata_only(text):
        return ""
    if len(text) < 80:
        return ""
    if _JD_BODY_MARKERS.search(text):
        return text
    if not is_noisy_jd(text):
        return _strip_page_chrome(text)
    # Paragraph-only extracts should not include nav chrome; accept if substantial
    if len(text) >= 300:
        return text
    return ""


async def _extract_page_listing_meta(page: Page) -> str:
    """Salary / location / policy strip on the job page (not the Apply modal)."""
    try:
        raw = await page.evaluate(
            """() => {
                const parts = [];
                const selectors = [
                    '[data-test="JobHeader"]',
                    '[data-test="Compensation"]',
                    '[class*="JobHeader"]',
                    '[class*="jobHeader"]',
                    '[class*="Compensation"]',
                    '[class*="compensation"]',
                    '[class*="Salary"]',
                    '[class*="salary"]',
                ];
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        const t = el.innerText?.trim();
                        if (t && t.length < 2500) parts.push(t);
                    });
                }
                const main = document.querySelector('[class*="styles_body"], [class*="JobHeader"], #root');
                if (main) {
                    const head = main.cloneNode(true);
                    head.querySelectorAll(
                        '[class*="styles_description"], p, li, [role="dialog"]'
                    ).forEach(n => n.remove());
                    const t = head.innerText.trim();
                    if (t && t.length < 3500) parts.push(t);
                }
                return [...new Set(parts)].join('\\n');
            }"""
        )
    except PlaywrightError:
        return ""
    return clean_jd_text(str(raw)) if raw else ""


async def _wait_for_app_ready(page: Page) -> None:
    """Wait for Wellfound's Next.js app to finish hydrating.

    Wellfound flags a fully-rendered page via ``<body data-tfe-status="ready">``;
    waiting for it avoids reading half-hydrated JD/company text.
    """
    with contextlib.suppress(PlaywrightTimeout, PlaywrightError):
        await page.wait_for_selector('body[data-tfe-status="ready"]', timeout=8000)


async def _wait_for_job_content(page: Page) -> None:
    await _wait_for_app_ready(page)
    for sel in (
        '[data-test="JobDescription"]',
        '[data-test="job-description"]',
        '[class*="styles_description"]',
        'div[class*="description__"]',
        '[class*="JobDescription"]',
        '[class*="jobDescription"]',
        "article p",
        "section p",
        "div p",
    ):
        try:
            await page.wait_for_selector(sel, timeout=8000)
            return
        except PlaywrightTimeout:
            continue
    await page.wait_for_timeout(2000)


async def _read_next_data(page: Page) -> WellfoundJobData | None:
    """Parse the page's embedded Apollo cache (clean, structured job detail)."""
    try:
        html = await page.content()
    except PlaywrightError:
        return None
    try:
        return parse_job_detail(html)
    except Exception as exc:  # never let a payload quirk break the apply flow
        logger.debug("Wellfound __NEXT_DATA__ parse failed: %s", exc)
        return None


async def extract_wellfound_job_page(page: Page, *, min_inr_lpa: float = 25.0) -> WellfoundApplyModal:
    """Read job detail from __NEXT_DATA__ (preferred) or the listing DOM (fallback)."""
    await _wait_for_job_content(page)
    await _scroll_job_page(page)

    nd = await _read_next_data(page)
    dom_jd = await _extract_wellfound_page_jd(page)
    # The Apollo `description` is the authoritative, fully-rendered JD; prefer it
    # whenever present and substantial, falling back to the DOM scrape otherwise.
    if nd and nd.description and len(nd.description) >= 120:
        jd = nd.description
        source = "__NEXT_DATA__"
    else:
        jd = dom_jd
        source = "job page DOM"

    meta_text = await _extract_page_listing_meta(page)
    salary_meta = "\n".join(s for s in ((nd.compensation if nd else ""), meta_text) if s)
    elig = eligibility_summary(
        combined_salary_text(jd=jd, modal=salary_meta),
        min_inr_lpa=min_inr_lpa,
    )
    if jd and not is_apply_metadata_only(jd):
        logger.info("JD from %s (%d chars)", source, len(jd))
    elif jd:
        logger.warning("JD from %s may be incomplete (%d chars)", source, len(jd))
    else:
        logger.warning("Could not extract JD from job page %s", page.url)
    return WellfoundApplyModal(
        modal_text=meta_text,
        jd=jd,
        eligibility=elig,
        opened=False,
        company=nd.company if nd else "",
        company_about=nd.company_about if nd else "",
        salary_display=nd.compensation if nd else "",
        skills=nd.skills if nd else [],
    )


async def _extract_paragraph_jd(page: Page) -> str:
    """Collect <p> and <li> text from main / JobDescription on the listing page."""
    try:
        raw = await page.evaluate(
            """() => {
                const dialog = document.querySelector('[role="dialog"]');
                const inDialog = (el) => dialog && (el === dialog || dialog.contains(el));
                const skip = (t) => {
                    if (!t || t.length < 15) return true;
                    if (/^apply to/i.test(t)) return true;
                    const skipPattern = new RegExp(
                        '^(remote only|in office|full time|visa sponsorship|not available|' +
                        'company location|job type|hires remotely)$',
                        'i'
                    );
                    if (skipPattern.test(t.trim())) return true;
                    return false;
                };
                const roots = [];
                for (const sel of [
                    '[data-test="JobDescription"]',
                    '[data-test="job-description"]',
                    '[class*="styles_description"]',
                    'div[class*="description__"]',
                    '[class*="JobDescription"]',
                    '[class*="jobDescription"]',
                    '[class*="job-description"]',
                    '#root',
                ]) {
                    document.querySelectorAll(sel).forEach(el => {
                        if (!inDialog(el)) roots.push(el);
                    });
                }
                let best = '';
                for (const root of roots) {
                    const parts = [...root.querySelectorAll('p, li')]
                        .filter(el => !inDialog(el))
                        .map(el => el.innerText.trim())
                        .filter(t => !skip(t));
                    const text = parts.join('\\n\\n');
                    if (text.length > best.length) best = text;
                }
                return best;
            }"""
        )
    except PlaywrightError:
        raw = ""

    candidates: list[str] = []
    if raw and isinstance(raw, str):
        finalized = _finalize_page_jd(str(raw))
        if finalized:
            candidates.append(finalized)

    for root_sel in PARA_ROOT_SELECTORS:
        roots = page.locator(root_sel)
        root_count = await roots.count()
        for r in range(min(root_count, 4)):
            try:
                root = roots.nth(r)
                paras = root.locator("p, li")
                parts: list[str] = []
                for i in range(await paras.count()):
                    try:
                        t = (await paras.nth(i).inner_text()).strip()
                        if _is_jd_paragraph(t):
                            parts.append(t)
                    except PlaywrightError:
                        continue
                if parts:
                    finalized = _finalize_page_jd(_join_jd_paragraphs(parts))
                    if finalized:
                        candidates.append(finalized)
            except PlaywrightError:
                continue

    if not candidates:
        return ""
    return max(candidates, key=len)


async def _extract_via_dom(page: Page) -> str:
    """Fallback: marker-based block or largest description container."""
    try:
        raw = await page.evaluate(
            """() => {
                const dialog = document.querySelector('[role="dialog"]');
                const inDialog = (el) => dialog && (el === dialog || dialog.contains(el));
                const markers = [
                    'Core Responsibilities',
                    'Required Experience',
                    'engineering team is solving',
                    'The role is',
                ];
                let best = '';
                const score = (t) => {
                    if (!t || t.length < 150) return 0;
                    if (t.trim().startsWith('APPLY TO')) return t.length * 0.2;
                    let s = t.length;
                    if (markers.some(m => t.includes(m))) s += 2000;
                    return s;
                };
                const selectors = [
                    '[data-test="JobDescription"]',
                    '[data-test="job-description"]',
                    '[class*="styles_description"]',
                    'div[class*="description__"]',
                    '[class*="JobDescription"]',
                    '[class*="jobDescription"]',
                    '[class*="job-description"]',
                    '#root article',
                    '#root section',
                    '#root',
                ];
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        if (inDialog(el)) return;
                        const t = el.innerText.trim();
                        if (score(t) > score(best)) best = t;
                    });
                }
                return best;
            }"""
        )
    except PlaywrightError:
        return ""
    if not raw:
        return ""
    return _finalize_page_jd(clean_jd_text(str(raw)))


async def _extract_wellfound_page_jd(page: Page) -> str:
    """Job description from <p>/<li> blocks on the listing page."""
    chunks: list[str] = []

    para_jd = await _extract_paragraph_jd(page)
    if para_jd:
        chunks.append(para_jd)

    dom_jd = await _extract_via_dom(page)
    if dom_jd and dom_jd not in chunks:
        chunks.append(dom_jd)

    for sel in WELLFOUND_PAGE_JD_SELECTORS:
        loc = page.locator(sel)
        count = await loc.count()
        for i in range(min(count, 5)):
            try:
                el = loc.nth(i)
                parts: list[str] = []
                paras = el.locator("p, li")
                for j in range(await paras.count()):
                    t = (await paras.nth(j).inner_text()).strip()
                    if _is_jd_paragraph(t):
                        parts.append(t)
                if parts:
                    text = _finalize_page_jd(_join_jd_paragraphs(parts))
                else:
                    text = _finalize_page_jd(clean_jd_text((await el.inner_text()).strip()))
                if text:
                    chunks.append(text)
            except PlaywrightError:
                continue

    if chunks:
        return max(chunks, key=len)

    try:
        desc = page.locator('[class*="styles_description"], div[class*="description__"]').first
        if await desc.count() > 0:
            text = _finalize_page_jd(clean_jd_text((await desc.inner_text()).strip()))
            if text:
                return text
    except PlaywrightError:
        pass
    return ""


async def _collect_modal_text(container) -> str:
    await _scroll_container(container)
    chunks: list[str] = []
    try:
        parts: list[str] = []
        paras = container.locator("p, li")
        for i in range(await paras.count()):
            try:
                t = (await paras.nth(i).inner_text()).strip()
                if _is_jd_paragraph(t):
                    parts.append(t)
            except PlaywrightError:
                continue
        if parts:
            chunks.append(_join_jd_paragraphs(parts))
        chunks.append(clean_jd_text((await container.inner_text()).strip()))
    except PlaywrightError:
        pass
    for sel in (
        '[class*="description" i]',
        '[class*="Description" i]',
        "article",
        "section",
    ):
        loc = container.locator(sel)
        count = await loc.count()
        for i in range(min(count, 4)):
            try:
                text = clean_jd_text((await loc.nth(i).inner_text()).strip())
                if len(text) > 150:
                    chunks.append(text)
            except PlaywrightError:
                continue
    return max(chunks, key=len) if chunks else ""


async def inspect_apply_modal(page: Page, *, min_inr_lpa: float = 25.0) -> WellfoundApplyModal:
    """Eligibility from Apply modal; JD always from the listing page."""
    page_jd = await _extract_wellfound_page_jd(page)
    meta_text = await _extract_page_listing_meta(page)

    container = await _modal_container(page)
    if container is None:
        elig = eligibility_summary(
            combined_salary_text(jd=page_jd, modal=meta_text),
            min_inr_lpa=min_inr_lpa,
        )
        return WellfoundApplyModal(opened=False, jd=page_jd, modal_text=meta_text, eligibility=elig)

    modal_text = await _collect_modal_text(container)
    elig = eligibility_summary(
        combined_salary_text(jd=page_jd, modal=modal_text or meta_text),
        min_inr_lpa=min_inr_lpa,
    )
    return WellfoundApplyModal(
        modal_text=modal_text or meta_text,
        jd=page_jd,
        eligibility=elig,
        opened=True,
    )


async def open_and_inspect_apply_modal(page: Page, *, min_inr_lpa: float = 25.0) -> WellfoundApplyModal:
    """Open Apply for eligibility checks; JD is read from the job page, not the modal."""
    page_info = await extract_wellfound_job_page(page, min_inr_lpa=min_inr_lpa)
    if not await click_apply(page):
        return page_info
    info = await inspect_apply_modal(page, min_inr_lpa=min_inr_lpa)
    info.jd = page_info.jd or info.jd
    return info
