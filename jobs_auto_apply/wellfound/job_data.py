"""Parse Wellfound's embedded ``__NEXT_DATA__`` Apollo state.

Wellfound is a Next.js app that ships the *entire* job + company record as clean
structured JSON inside ``<script id="__NEXT_DATA__">`` (the Apollo cache). Reading
that is dramatically more reliable than scraping the rendered DOM for the job
description, company name, "about" blurb, salary, experience, location, remote
policy and visa sponsorship — none of which depend on fragile hashed CSS classes.

DOM scraping remains the fallback when the payload is missing (e.g. a server-side
rendering variant or a future markup change).
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("job_apply")

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


@dataclass
class WellfoundJobData:
    """Structured job detail pulled from the Apollo cache."""

    job_id: str = ""
    title: str = ""
    description: str = ""
    company: str = ""
    company_about: str = ""
    company_url: str = ""
    company_size: str = ""
    compensation: str = ""
    equity: str = ""
    years_experience_min: int | None = None
    location_names: list[str] = field(default_factory=list)
    remote: bool = False
    remote_kind: str = ""
    visa_sponsorship: bool = False
    skills: list[str] = field(default_factory=list)
    markets: list[str] = field(default_factory=list)


def _strip_html(value: str) -> str:
    """Convert a small HTML fragment (atGlance, descriptionHtml) to plain text."""
    if not value:
        return ""
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|ul|ol|br)\s*>", "\n", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _markdown_to_text(value: str) -> str:
    """Lightly de-markdown the JobListing.description field into readable text."""
    if not value:
        return ""
    text = value.replace("\r\n", "\n")
    text = re.sub(r"^\s*\*\s+", "- ", text, flags=re.M)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = text.replace("**", "")
    text = re.sub(r"(?<=\w)\*(?=\w)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _resolve(node: Any, cache: dict[str, Any]) -> Any:
    """Resolve a single Apollo ``{"__ref": ...}`` pointer to its cached object."""
    if isinstance(node, dict) and "__ref" in node:
        return cache.get(node["__ref"], {})
    return node


def _display_names(refs: Any, cache: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for ref in refs or []:
        obj = _resolve(ref, cache)
        if isinstance(obj, dict):
            name = str(obj.get("displayName") or obj.get("name") or "").strip()
            if name:
                out.append(name)
    return out


def _find_job_listing(cache: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    if job_id:
        direct = cache.get(f"JobListing:{job_id}")
        if isinstance(direct, dict):
            return direct
    # Fall back to the richest JobListing present (the detail record carries a
    # full `description`; similar-job cards only have `descriptionSnippet`).
    best: dict[str, Any] | None = None
    for key, value in cache.items():
        if not key.startswith("JobListing:") or not isinstance(value, dict):
            continue
        if value.get("description") and (
            best is None or len(str(value.get("description", ""))) > len(str(best.get("description", "")))
        ):
            best = value
    return best


def parse_job_detail(page_html: str, *, job_id: str = "") -> WellfoundJobData | None:
    """Extract the job-detail record from a page's ``__NEXT_DATA__`` payload."""
    if not page_html:
        return None
    match = _NEXT_DATA_RE.search(page_html)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except (ValueError, TypeError):
        return None

    page_props = payload.get("props", {}).get("pageProps", {})
    apollo = page_props.get("apolloState", {})
    cache: dict[str, Any] = apollo.get("data", apollo) if isinstance(apollo, dict) else {}
    if not isinstance(cache, dict) or not cache:
        return None

    if not job_id:
        job_id = str(page_props.get("params", {}).get("jobId") or "")

    listing = _find_job_listing(cache, job_id)
    if not isinstance(listing, dict):
        return None

    startup = _resolve(listing.get("startup"), cache) if listing.get("startup") else {}
    startup = startup if isinstance(startup, dict) else {}

    description = _markdown_to_text(str(listing.get("description") or ""))
    if not description and listing.get("descriptionHtml"):
        description = _strip_html(str(listing.get("descriptionHtml")))

    about = _strip_html(str(startup.get("atGlance") or ""))
    high_concept = str(startup.get("highConcept") or "").strip()
    if high_concept and high_concept not in about:
        about = (high_concept + ("\n\n" + about if about else "")).strip()

    remote_kind = ""
    remote_cfg = _resolve(listing.get("remoteConfig"), cache) if listing.get("remoteConfig") else {}
    if isinstance(remote_cfg, dict):
        remote_kind = str(remote_cfg.get("kind") or "")

    years_min = listing.get("yearsExperienceMin")
    try:
        years_min = int(years_min) if years_min is not None else None
    except (TypeError, ValueError):
        years_min = None

    data = WellfoundJobData(
        job_id=str(listing.get("id") or job_id),
        title=str(listing.get("title") or listing.get("primaryRoleTitle") or "").strip(),
        description=description,
        company=str(startup.get("name") or "").strip(),
        company_about=about[:2000],
        company_url=str(startup.get("companyUrl") or "").strip(),
        company_size=str(startup.get("companySize") or "").strip(),
        compensation=str(listing.get("compensation") or "").strip(),
        equity=str(listing.get("equity") or "").strip(),
        years_experience_min=years_min,
        location_names=[str(x) for x in (listing.get("locationNames") or []) if x],
        remote=bool(listing.get("remote")),
        remote_kind=remote_kind,
        visa_sponsorship=bool(listing.get("visaSponsorship")),
        skills=_display_names(listing.get("skills"), cache),
        markets=_display_names(startup.get("marketTaggings"), cache),
    )
    return data
