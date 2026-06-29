from __future__ import annotations

import logging
import re

from .utils import JobListing

logger = logging.getLogger("job_apply")

# Example "keep" anchors for a backend/platform engineering search. NOT applied by
# default — the filter is domain-neutral unless you set profile.keep_role_keywords.
# Copy any of these into config.yaml (or write your own, e.g. legal/finance terms).
# Matched as whole words/phrases, case-insensitively.
DEFAULT_KEEP_ROLE_KEYWORDS: tuple[str, ...] = (
    "backend",
    "back end",
    "back-end",
    "platform",
    "python developer",
    "java developer",
    "nodejs backend",
    "node.js backend",
)


def _title_patterns(
    keywords: list[str],
    *,
    skip_patterns: list[str] | None = None,
) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for raw in skip_patterns or []:
        text = (raw or "").strip()
        if not text:
            continue
        try:
            patterns.append(re.compile(text, re.I))
        except re.error as exc:
            logger.warning("Ignoring invalid skip_role_patterns regex %r: %s", raw, exc)
    for kw in keywords:
        text = kw.strip()
        if not text:
            continue
        patterns.append(re.compile(_keyword_regex(text), re.I))
    return patterns


def _keep_regex(keep_keywords: list[str] | tuple[str, ...] | None) -> re.Pattern[str] | None:
    """Build a single case-insensitive regex from the keep keywords (or None).

    No implicit defaults: when no keep keywords are configured, there are no keep
    anchors (returns None) so the filter stays domain-neutral.
    """
    source = keep_keywords or []
    parts = [_keyword_regex(str(k).strip()) for k in source if str(k).strip()]
    if not parts:
        return None
    return re.compile("|".join(parts), re.I)


def role_filter_kwargs(profile) -> dict[str, object]:
    """Bundle a profile's role-filter settings for should_skip_role / filter_skipped_roles.

    Lets every call site stay in sync with config via a single ``**role_filter_kwargs(...)``
    instead of repeating each keyword argument.
    """
    return {
        "keywords": profile.skip_role_keywords,
        "keep_keywords": profile.keep_role_keywords,
        "skip_patterns": profile.skip_role_patterns,
    }


def _keyword_regex(text: str) -> str:
    """Word-boundary-wrapped literal so short keywords (e.g. 'sre') don't match
    substrings inside unrelated words."""
    body = re.escape(text)
    prefix = r"\b" if text[:1].isalnum() else ""
    suffix = r"\b" if text[-1:].isalnum() else ""
    return f"{prefix}{body}{suffix}"


def should_skip_role(
    title: str,
    *,
    keywords: list[str] | None = None,
    keep_keywords: list[str] | None = None,
    skip_patterns: list[str] | None = None,
    jd: str = "",
) -> tuple[bool, str]:
    """Return (skip, reason).

    A title matching any ``keep_keywords`` anchor is kept; otherwise it is skipped
    when it matches any custom ``skip_patterns`` regex or any ``keywords`` term.
    All inputs are config-driven, so the same machinery can target backend,
    frontend, or any other role family in any domain.
    """
    title = (title or "").strip()
    if not title:
        return False, ""

    keep_re = _keep_regex(keep_keywords)

    # A keep-anchor rescues the title from all skip rules.
    if keep_re and keep_re.search(title):
        return False, ""

    for pat in _title_patterns(
        keywords or [],
        skip_patterns=skip_patterns,
    ):
        if pat.search(title):
            return True, f"role filter: {title!r}"

    return False, ""


def should_skip_no_experience_role(
    title: str,
    *,
    no_exp_skills: list[str],
    known_skills: list[str],
) -> tuple[bool, str]:
    """Skip a title that is about a no-experience skill and names no known skill.

    Returns (skip, reason). The title must mention one of ``no_exp_skills`` and NOT
    mention any of ``known_skills`` — so "Salesforce Developer" is skipped while
    "Java Developer (Salesforce integration)" is kept (Java is a known skill).
    """
    title = (title or "").strip()
    if not title or not no_exp_skills:
        return False, ""
    matched = next(
        (s for s in no_exp_skills if s.strip() and re.search(_keyword_regex(s.strip()), title, re.I)),
        None,
    )
    if not matched:
        return False, ""
    for known in known_skills or []:
        k = known.strip()
        if k and re.search(_keyword_regex(k), title, re.I):
            return False, ""
    return True, f"no-experience skill {matched!r}: {title!r}"


def filter_no_experience_roles(
    jobs: list[JobListing],
    *,
    no_exp_skills: list[str] | None = None,
    known_skills: list[str] | None = None,
) -> list[JobListing]:
    if not no_exp_skills:
        return jobs
    kept: list[JobListing] = []
    for job in jobs:
        skip, reason = should_skip_no_experience_role(
            job.title,
            no_exp_skills=no_exp_skills,
            known_skills=known_skills or [],
        )
        if skip:
            logger.info("Skipping role: %s — %s", job.title, reason)
            continue
        kept.append(job)
    return kept


def filter_skipped_roles(
    jobs: list[JobListing],
    *,
    keywords: list[str] | None = None,
    keep_keywords: list[str] | None = None,
    skip_patterns: list[str] | None = None,
) -> list[JobListing]:
    if not keywords and not skip_patterns:
        return jobs
    kept: list[JobListing] = []
    for job in jobs:
        skip, reason = should_skip_role(
            job.title,
            keywords=keywords,
            keep_keywords=keep_keywords,
            skip_patterns=skip_patterns,
            jd=job.description,
        )
        if skip:
            logger.info("Skipping role: %s — %s", job.title, reason)
            continue
        kept.append(job)
    return kept


def auto_reject_skipped_roles(
    items,
    *,
    keywords: list[str] | None = None,
    keep_keywords: list[str] | None = None,
    skip_patterns: list[str] | None = None,
) -> int:
    """Mark pending review items as rejected when they match skip rules. Returns count rejected."""
    count = 0
    for item in items:
        if item.status != "pending":
            continue
        skip, reason = should_skip_role(
            item.title,
            keywords=keywords,
            keep_keywords=keep_keywords,
            skip_patterns=skip_patterns,
            jd=getattr(item, "jd_excerpt", "") or "",
        )
        if skip:
            item.status = "rejected"
            logger.info("Auto-rejected role: %s — %s", item.title, reason)
            count += 1
    return count
