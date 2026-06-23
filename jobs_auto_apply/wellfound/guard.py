from __future__ import annotations

import asyncio
import logging
import random
import re

from playwright.async_api import Page

from ..apply_runner import ApplyBatchStopped

logger = logging.getLogger("job_apply")

RESTRICTED_TEXT = re.compile(
    r"access is temporarily restricted|temporarily restricted|"
    r"unusual traffic|verify you are human|access denied",
    re.I,
)

APPLICATION_LIMIT_TEXT = re.compile(
    r"maximum number of active applications|"
    r"reached the maximum.*applications|"
    r"too many active applications|"
    r"too many applications|"
    r"sorry.{0,60}(maximum|too many).{0,40}applications?",
    re.I,
)

APPLY_SUCCESS_TEXT = re.compile(
    r"application sent|successfully applied|application submitted|"
    r"your application has been|thanks for applying|we('ve| have) received your application",
    re.I,
)


class WellfoundAccessRestricted(Exception):
    """Wellfound / DataDome blocked this session."""


class WellfoundApplicationLimitReached(ApplyBatchStopped):
    """Wellfound blocked further applies — active application cap hit."""


async def is_access_restricted(page: Page) -> bool:
    try:
        if page.is_closed():
            return False
        text = await page.title()
        if RESTRICTED_TEXT.search(text):
            return True
        body = await page.locator("body").inner_text(timeout=3000)
        return bool(RESTRICTED_TEXT.search(body))
    except Exception:
        return False


async def is_application_limit_reached(page: Page) -> bool:
    try:
        if page.is_closed():
            return False
        body = await page.locator("body").inner_text(timeout=3000)
        if APPLICATION_LIMIT_TEXT.search(body):
            return True
        for pattern in (
            r"maximum number of active applications",
            r"reached the maximum",
            r"too many applications",
            r"too many active applications",
        ):
            loc = page.get_by_text(re.compile(pattern, re.I))
            if await loc.count() > 0:
                try:
                    if await loc.first.is_visible():
                        return True
                except Exception:
                    pass
        return False
    except Exception:
        return False


async def wellfound_apply_succeeded(page: Page) -> bool:
    """True when the page shows a real submit confirmation (not just button click)."""
    try:
        if page.is_closed():
            return False
        body = await page.locator("body").inner_text(timeout=3000)
        if APPLY_SUCCESS_TEXT.search(body):
            return True
        applied = page.locator("button, a").filter(
            has_text=re.compile(r"^applied$", re.I)
        )
        if await applied.count() > 0:
            try:
                return await applied.first.is_visible()
            except Exception:
                pass
        # Modal closed and Send Application gone — weak signal only if no apply form left
        send = page.get_by_role("button", name=re.compile(r"send application", re.I))
        if await send.count() > 0:
            try:
                if await send.first.is_visible():
                    return False
            except Exception:
                pass
        easy_apply = page.get_by_role("button", name=re.compile(r"^apply$", re.I))
        if await easy_apply.count() > 0:
            try:
                if await easy_apply.first.is_visible():
                    return False
            except Exception:
                pass
        return False
    except Exception:
        return False


async def resolve_post_submit(page: Page) -> str:
    """
    Inspect page after clicking Send Application.
    Returns 'limit', 'success', or 'failed'.
    """
    await page.wait_for_timeout(1500)
    if await is_application_limit_reached(page):
        return "limit"
    if await wellfound_apply_succeeded(page):
        return "success"
    return "failed"


async def assert_not_restricted(page: Page) -> None:
    if await is_access_restricted(page):
        raise WellfoundAccessRestricted(
            "Wellfound shows 'Access is temporarily restricted' — "
            "stop the run, wait 30–60 minutes, then retry with fewer workers and delays."
        )


async def job_delay_seconds(config) -> float:
    lo = max(0, config.application.delay_seconds_min)
    hi = max(lo, config.application.delay_seconds_max)
    if hi <= 0:
        return 0.0
    return random.uniform(lo, hi)


async def pause_between_jobs(page: Page, config) -> None:
    seconds = await job_delay_seconds(config)
    if seconds > 0:
        await page.wait_for_timeout(int(seconds * 1000))
