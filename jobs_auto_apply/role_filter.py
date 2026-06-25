from __future__ import annotations

import logging
import re

from .utils import JobListing

logger = logging.getLogger("job_apply")

# Titles that are clearly backend/platform even if they mention a UI stack.
# NOTE: deliberately excludes devops/sre/infra so those can be skipped via keywords.
BACKEND_TITLE_HINT = re.compile(
    r"\b(backend|back[\s-]?end|platform|"
    r"python developer|java developer|node\.?js backend)\b",
    re.I,
)

DEFAULT_FRONTEND_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"front[\s-]?end", re.I),
    re.compile(r"\bui\s*/?\s*ux\b", re.I),
    re.compile(r"\bui\s+engineer\b", re.I),
    re.compile(r"\b(react|angular|vue|svelte|next\.?js)\s+(developer|engineer|dev)\b", re.I),
    re.compile(r"\b(developer|engineer|dev|sde|architect)\s*/\s*(react|angular|vue)\b", re.I),
    re.compile(r"\breact\s+native\b", re.I),
    re.compile(r"\bfrontend\s+(developer|engineer|dev)\b", re.I),
    re.compile(r"\bfull[\s-]?stack\b.{0,50}\b(react|angular|vue)\b", re.I),
    re.compile(r"\b(react|angular|vue)\s*/\s*\w+", re.I),
    re.compile(r"\bangular\s+developer\b", re.I),
]

DEFAULT_QA_TEST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bqa\b", re.I),
    re.compile(r"\bquality assurance\b", re.I),
    re.compile(r"\bqae?\b", re.I),
    re.compile(r"\bsdet\b", re.I),
    re.compile(r"\bste\b", re.I),
    re.compile(r"\btester\b", re.I),
    re.compile(r"\btest\s+(engineer|lead|manager|analyst|specialist|architect)\b", re.I),
    re.compile(r"\b(engineer|developer|analyst)\s*/\s*test(ing)?\b", re.I),
    re.compile(r"\bautomation\s+test", re.I),
    re.compile(r"\btest\s+automation\b", re.I),
    re.compile(r"\bmanual\s+test", re.I),
    re.compile(r"\btesting\s+engineer\b", re.I),
]


def _title_patterns(
    keywords: list[str],
    *,
    skip_frontend: bool,
    skip_qa_test: bool,
) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    if skip_frontend:
        patterns.extend(DEFAULT_FRONTEND_PATTERNS)
    if skip_qa_test:
        patterns.extend(DEFAULT_QA_TEST_PATTERNS)
    for kw in keywords:
        text = kw.strip()
        if not text:
            continue
        patterns.append(re.compile(_keyword_regex(text), re.I))
    return patterns


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
    skip_frontend: bool = True,
    skip_qa_test: bool = True,
    keywords: list[str] | None = None,
    jd: str = "",
) -> tuple[bool, str]:
    """Return (skip, reason). Skips frontend/UI and QA/test roles; keeps backend/platform titles."""
    title = (title or "").strip()
    if not title:
        return False, ""

    if BACKEND_TITLE_HINT.search(title) and not re.search(r"front[\s-]?end", title, re.I):
        return False, ""

    for pat in _title_patterns(keywords or [], skip_frontend=skip_frontend, skip_qa_test=skip_qa_test):
        if pat.search(title):
            return True, f"role filter: {title!r}"

    # Strong frontend signal in JD when title is generic
    if skip_frontend and jd and not BACKEND_TITLE_HINT.search(title):
        jd_head = jd[:1200].lower()
        if (
            re.search(r"front[\s-]?end", jd_head)
            and re.search(r"\b(react|angular|vue|typescript|ui/ux)\b", jd_head)
            and not re.search(r"\b(backend|python|java|fastapi|spring)\b", jd_head)
        ):
            return True, "role filter: JD is frontend-focused"

    if skip_qa_test and jd and not BACKEND_TITLE_HINT.search(title):
        jd_head = jd[:1500]
        if re.search(
            r"\b(quality assurance|test automation|manual testing|automation testing|"
            r"software testing|qa engineer|sdet)\b",
            jd_head,
            re.I,
        ):
            return True, "role filter: JD is QA/test-focused"

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
        (
            s for s in no_exp_skills
            if s.strip() and re.search(_keyword_regex(s.strip()), title, re.I)
        ),
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
    skip_frontend: bool = True,
    skip_qa_test: bool = True,
    keywords: list[str] | None = None,
) -> list[JobListing]:
    if not skip_frontend and not skip_qa_test and not keywords:
        return jobs
    kept: list[JobListing] = []
    for job in jobs:
        skip, reason = should_skip_role(
            job.title,
            skip_frontend=skip_frontend,
            skip_qa_test=skip_qa_test,
            keywords=keywords,
            jd=job.description,
        )
        if skip:
            logger.info("Skipping role: %s — %s", job.title, reason)
            continue
        kept.append(job)
    return kept


def filter_skipped_review_titles(
    items,
    *,
    skip_frontend: bool = True,
    skip_qa_test: bool = True,
    keywords: list[str] | None = None,
):
    """Filter review items by title/JD."""
    kept = []
    for item in items:
        skip, reason = should_skip_role(
            item.title,
            skip_frontend=skip_frontend,
            skip_qa_test=skip_qa_test,
            keywords=keywords,
            jd=getattr(item, "jd_excerpt", "") or "",
        )
        if skip:
            logger.info("Skipping role: %s — %s", item.title, reason)
            continue
        kept.append(item)
    return kept


def auto_reject_skipped_roles(
    items,
    *,
    skip_frontend: bool = True,
    skip_qa_test: bool = True,
    keywords: list[str] | None = None,
) -> int:
    """Mark pending review items as rejected when they match skip rules. Returns count rejected."""
    count = 0
    for item in items:
        if item.status != "pending":
            continue
        skip, reason = should_skip_role(
            item.title,
            skip_frontend=skip_frontend,
            skip_qa_test=skip_qa_test,
            keywords=keywords,
            jd=getattr(item, "jd_excerpt", "") or "",
        )
        if skip:
            item.status = "rejected"
            logger.info("Auto-rejected role: %s — %s", item.title, reason)
            count += 1
    return count
