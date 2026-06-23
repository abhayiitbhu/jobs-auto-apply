from __future__ import annotations

import logging
import random
import re

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from ..ats.apply import apply_on_company_site
from ..config import AppConfig
from ..cookies import is_external_career_url
from ..page_load import goto_settled
from ..utils import JobListing, job_key, save_applied_job

logger = logging.getLogger("job_apply")

EXTERNAL_HOST_HINTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "icims.com",
    "bamboohr.com",
    "careers.",
    "jobs.",
)


async def resolve_external_apply_url(page: Page, context: BrowserContext, job: JobListing) -> str | None:
    """Open Uplers job page and capture the company career-site URL."""
    await goto_settled(page, job.url)

    # Direct external links on the detail page.
    for link in await page.locator("a[href]").all():
        href = await link.get_attribute("href") or ""
        if not href.startswith("http"):
            continue
        if is_external_career_url(href) or any(h in href.lower() for h in EXTERNAL_HOST_HINTS):
            logger.info("Found external apply URL on page: %s", href)
            return href

    apply_controls = page.get_by_role(
        "button",
        name=re.compile(r"apply|view job|visit|company website|career", re.I),
    )
    if await apply_controls.count() == 0:
        apply_controls = page.get_by_role("link", name=re.compile(r"apply|view job|visit|career", re.I))

    if await apply_controls.count() == 0:
        logger.warning("No apply control on Uplers page: %s", job.url)
        return None

    control = apply_controls.first
    href = await control.get_attribute("href")
    if href and href.startswith("http") and is_external_career_url(href):
        return href

    try:
        async with context.expect_page(timeout=12000) as new_page_info:
            await control.click()
        new_page = await new_page_info.value
        await new_page.wait_for_load_state("domcontentloaded")
        await new_page.wait_for_timeout(1500)
        external_url = new_page.url
        if is_external_career_url(external_url):
            await page.goto(external_url, wait_until="domcontentloaded")
            return external_url
    except PlaywrightTimeout:
        pass

    try:
        await control.click()
        await page.wait_for_timeout(2500)
        if is_external_career_url(page.url):
            return page.url
    except PlaywrightTimeout:
        logger.warning("Could not navigate to external site for %s", job.url)

    return None


async def apply_to_uplers_job(
    page: Page,
    context: BrowserContext,
    job: JobListing,
    config: AppConfig,
) -> bool:
    external_url = job.external_url or await resolve_external_apply_url(page, context, job)
    if not external_url:
        return False

    if config.application.dry_run:
        logger.info("[DRY RUN] Would apply via %s for %s @ %s", external_url, job.title, job.company)
        return False

    success = await apply_on_company_site(page, job=job, config=config, url=external_url)
    if not success:
        return False

    save_applied_job(
        config.applied_jobs_path,
        job_key("uplers", job.job_id),
        {
            "source": "uplers",
            "title": job.title,
            "company": job.company,
            "uplers_url": job.url,
            "external_url": external_url,
        },
    )
    logger.info("Applied via company site: %s @ %s", job.title, job.company)
    return True


async def apply_batch(
    page: Page,
    context: BrowserContext,
    jobs: list[JobListing],
    config: AppConfig,
) -> int:
    applied = 0
    for job in jobs:
        try:
            if await apply_to_uplers_job(page, context, job, config):
                applied += 1
        except Exception:
            logger.exception("Failed Uplers apply for %s", job.url)

        delay = random.randint(
            config.application.delay_seconds_min,
            config.application.delay_seconds_max,
        )
        logger.info("Waiting %ds before next application...", delay)
        await page.wait_for_timeout(delay * 1000)
    return applied
