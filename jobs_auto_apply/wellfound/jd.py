from __future__ import annotations

from playwright.async_api import Page

from .modal import (
    extract_wellfound_job_page,
)


async def extract_wellfound_page_jd(page: Page) -> str:
    from .modal import _extract_wellfound_page_jd

    return await _extract_wellfound_page_jd(page)


async def extract_wellfound_job_description(page: Page, *, max_chars: int = 12000, min_inr_lpa: float = 25.0) -> str:
    info = await extract_wellfound_job_page(page, min_inr_lpa=min_inr_lpa)
    return info.jd[:max_chars] if info.jd else ""
