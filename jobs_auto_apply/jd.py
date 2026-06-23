from __future__ import annotations

import logging
import re

from playwright.async_api import Page

logger = logging.getLogger("job_apply")

JD_SELECTORS = (
    '[class*="job-description" i]',
    '[class*="jobDescription" i]',
    '[class*="job-desc" i]',
    '[data-testid*="description" i]',
    '[id*="jobDescription" i]',
    '[id*="job-description" i]',
    ".jd-info",
    ".job-details",
    ".description",
    "section.description",
    "#jobDescriptionText",
    '[class*="JobDescription"]',
    "article",
    "main",
)

NOISE_PATTERNS = re.compile(
    r"(apply now|sign in|similar jobs|copyright|©|all rights reserved|cookie)",
    re.I,
)

PAGE_CHROME_NOISE = re.compile(
    r"RECENT NOTIFICATIONS|Refer a friend|Ready to interview|"
    r"Home\s*\n\s*Profile\s*\n\s*Jobs|DiscoverStartups|Earn \$200|"
    r"Job Applications \| Wellfound",
    re.I,
)


def is_noisy_jd(text: str) -> bool:
    if not text or len(text) < 80:
        return True
    return bool(PAGE_CHROME_NOISE.search(text[:2500]))


def clean_jd_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


async def extract_job_description(page: Page, *, max_chars: int = 6000) -> str:
    """Scrape visible job description text from the current page."""
    chunks: list[str] = []

    for selector in JD_SELECTORS:
        loc = page.locator(selector)
        count = await loc.count()
        for i in range(min(count, 3)):
            try:
                el = loc.nth(i)
                if not await el.is_visible():
                    continue
                text = (await el.inner_text()).strip()
                if len(text) > 120 and not NOISE_PATTERNS.search(text[:200]):
                    chunks.append(text)
            except Exception:
                continue
        if chunks:
            break

    if not chunks:
        try:
            body = await page.locator("body").inner_text()
            if body and not is_noisy_jd(body):
                chunks.append(body)
        except Exception:
            return ""

    if not chunks:
        return ""

    jd = max(chunks, key=len)
    jd = clean_jd_text(jd)
    if is_noisy_jd(jd):
        return ""
    return jd[:max_chars]
