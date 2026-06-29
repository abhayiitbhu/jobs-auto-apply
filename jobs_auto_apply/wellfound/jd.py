from __future__ import annotations

from playwright.async_api import Page


async def extract_wellfound_page_jd(page: Page) -> str:
    from .modal import _extract_wellfound_page_jd

    return await _extract_wellfound_page_jd(page)
