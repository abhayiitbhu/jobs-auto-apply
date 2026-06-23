from __future__ import annotations

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
    if re.search(r"\d+\s*[kK]|\d+L|LPA|%\s*–|•\s*\d", text):
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
    if re.match(r"in\s*office", lower):
        return True
    return False


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
        'a[href*="/company/"]',
        '[data-test="startup-link"]',
        "h2 a",
    ):
        loc = page.locator(sel)
        count = await loc.count()
        for i in range(min(count, 5)):
            text = (await loc.nth(i).inner_text()).strip()
            if text and not looks_like_location_not_company(text):
                return text

    return parse_company_from_jd(title, jd)


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
