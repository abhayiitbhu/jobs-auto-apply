from __future__ import annotations

import contextlib
import re

from playwright.async_api import Page

# Location / compensation labels that appear on Wellfound cards — not company names.
_BAD_COMPANY_RE = re.compile(
    r"^(remote only|in office|onsite or remote|onsite|hybrid|everywhere|"
    r"no equity|posted|save|apply|actively hiring|discover|overview|people|jobs)$",
    re.I,
)


def looks_like_location_not_company(text: str) -> bool:
    text = text.strip()
    if not text or len(text) < 2:
        return True
    if _BAD_COMPANY_RE.match(text):
        return True
    if re.search(r"[$₹£€]", text):
        return True
    if re.search(r"\d+\s*[kK]|\d+L|LPA|%\s*-|•\s*\d", text):
        return True
    lower = text.lower()
    if any(
        phrase in lower
        for phrase in (
            "remote only",
            "in office",
            "onsite or remote",
            "remote (",
            "everywhere",
            "more₹",
            "more$",
        )
    ):
        return True
    return bool(re.match(r"in\s*office", lower))


def parse_company_from_jd(title: str, jd: str) -> str:
    """Extract startup name from scraped Wellfound job page text."""
    if title:
        m = re.search(
            rf"{re.escape(title)}\s+at\s+(.+?)(?:\n|₹|\$|£|€|POSTED|Save|Apply)",
            jd,
            re.I | re.S,
        )
        if m:
            name = m.group(1).strip()
            if not looks_like_location_not_company(name):
                return name

    m = re.search(r"DiscoverStartups\s*([^\n]+?)\n\1\n", jd)
    if m:
        name = m.group(1).strip()
        if not looks_like_location_not_company(name):
            return name

    m = re.search(r"\n([A-Za-z][^\n]{1,80})\n(?:Actively Hiring|RECRUITER RECENTLY ACTIVE)", jd)
    if m:
        name = m.group(1).strip()
        if not looks_like_location_not_company(name):
            return name

    return ""


async def extract_wellfound_company(page: Page, title: str, jd: str) -> str:
    for sel in (
        '[data-test="startup-link"]',
        '[data-test="StartupName"]',
        '[data-test="startup-name"]',
        'a[href*="/company/"]',
        "h2 a",
    ):
        loc = page.locator(sel)
        count = await loc.count()
        for i in range(min(count, 5)):
            text = (await loc.nth(i).inner_text()).strip()
            if text and not looks_like_location_not_company(text):
                return text

    return parse_company_from_jd(title, jd)


_ABOUT_HEADING = re.compile(
    r"\babout\s+(?:us|the\s+company|the\s+team|the\s+startup|our\s+company)\b"
    r"|\bwho\s+we\s+are\b|\bour\s+(?:mission|story)\b|\bcompany\s+(?:overview|description)\b",
    re.I,
)


def about_from_text(jd: str) -> str:
    """Best-effort 'about the company' slice from scraped job-page text."""
    if not jd:
        return ""
    # Prefer an explicit "About us / Who we are / Our mission" heading and grab the
    # paragraph(s) that follow, up to the next blank line.
    m = _ABOUT_HEADING.search(jd)
    if m:
        tail = jd[m.end() :]
        body = re.search(r"[:\s]*\n+(.{80,1500}?)(?:\n\s*\n|\Z)", tail, re.S)
        if body:
            return re.sub(r"\s+", " ", body.group(1)).strip()[:1200]
        inline = re.search(r"[:\s]+(.{80,1500}?)(?:\n\s*\n|\Z)", tail, re.S)
        if inline:
            return re.sub(r"\s+", " ", inline.group(1)).strip()[:1200]
    m = re.search(
        r"\babout\b[^\n]{0,40}\n+(.{80,1000}?)(?:\n\s*\n|\Z)",
        jd,
        re.I | re.S,
    )
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()[:1200]
    return ""


async def extract_wellfound_company_about(page: Page, *, jd: str = "") -> str:
    """Company 'about' / overview text from the Wellfound job page (JD fallback)."""
    for sel in (
        '[data-test="StartupDescription"]',
        '[data-test="startup-description"]',
        '[data-test="CompanyDescription"]',
        '[data-test="AboutCompany"]',
        '[data-test="about-company"]',
        '[class*="styles_about" i]',
        '[class*="aboutCompany" i]',
        '[class*="companyDescription" i]',
        '[class*="startupDescription" i]',
        'div[class*="description"]',
    ):
        loc = page.locator(sel)
        with contextlib.suppress(Exception):
            if await loc.count() > 0:
                text = (await loc.first.inner_text()).strip()
                if len(text) > 60:
                    return re.sub(r"\s+", " ", text).strip()[:1200]

    # Heading-anchored fallback: find an "About …" / "Who we are" heading in the DOM
    # and read the sibling content block.
    with contextlib.suppress(Exception):
        text = await page.evaluate(
            r"""() => {
                const re = /about\s+(us|the\s+company|the\s+team|the\s+startup|our\s+company)|who\s+we\s+are|our\s+(mission|story)|company\s+(overview|description)/i;
                const heads = [...document.querySelectorAll('h1, h2, h3, h4, [class*="heading" i], [class*="title" i]')];
                for (const h of heads) {
                    const t = (h.innerText || '').trim();
                    if (!re.test(t) || t.length > 80) continue;
                    let node = h.nextElementSibling;
                    const parts = [];
                    while (node && parts.join(' ').length < 1400) {
                        if (/^h[1-4]$/i.test(node.tagName)) break;
                        const txt = (node.innerText || '').trim();
                        if (txt) parts.push(txt);
                        node = node.nextElementSibling;
                    }
                    let body = parts.join('\n').trim();
                    if (body.length < 60 && h.parentElement) {
                        body = (h.parentElement.innerText || '').replace(t, '').trim();
                    }
                    if (body.length >= 60) return body;
                }
                return '';
            }"""
        )
        if text and len(str(text).strip()) > 60:
            return re.sub(r"\s+", " ", str(text)).strip()[:1200]

    return about_from_text(jd)


def repair_cover_letter_company(cover_letter: str, *, old_company: str, new_company: str) -> str:
    if not cover_letter or not new_company:
        return cover_letter
    if old_company and old_company != new_company:
        cover_letter = cover_letter.replace(f"Hi {old_company} team", f"Hi {new_company} team")
        cover_letter = cover_letter.replace(f"at {old_company}", f"at {new_company}")
    if "your organisation" not in cover_letter and new_company not in cover_letter:
        cover_letter = re.sub(
            r"at your organisation",
            f"at {new_company}",
            cover_letter,
            count=1,
        )
    return cover_letter
