from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from .config import AppConfig
from .cover_letter import _match_skills, _pick_roles_for_jd
from .profile_data import ResumeFacts, load_resume_facts
from .salary import eligibility_summary, is_job_salary_eligible, job_eligibility, parse_salary_ranges
from .utils import company_key

logger = logging.getLogger("job_apply")

T = TypeVar("T", bound="ScorableJob")


class ScorableJob(Protocol):
    job_id: str
    title: str
    company: str
    meta: dict[str, Any]


@dataclass
class JobFitScore:
    total: float
    salary: float
    profile: float
    eligible: bool
    reason: str


def job_text(job: ScorableJob) -> str:
    jd = getattr(job, "jd_excerpt", "") or getattr(job, "description", "") or ""
    meta = job.meta or {}
    parts = [jd]
    if meta.get("salary_display"):
        parts.append(str(meta["salary_display"]))
    return "\n".join(p for p in parts if p)


def _salary_component(text: str, meta: dict[str, Any], *, min_inr_lpa: float) -> tuple[float, str]:
    if meta.get("location_blocked"):
        return 0.0, "location blocked"
    if meta.get("salary_eligible") is False:
        return 1.0, meta.get("salary_reason", "low salary")

    elig = meta if "salary_eligible" in meta else eligibility_summary(text, min_inr_lpa=min_inr_lpa)
    if not elig.get("salary_eligible", True):
        return 1.0, elig.get("salary_reason", "low salary")

    ranges = parse_salary_ranges(text)
    if not ranges:
        return 6.0, "no salary listed"

    if any(r.currency != "INR" for r in ranges):
        return 9.0, f"non-INR ({ranges[0].raw})"

    max_inr = max(r.max_inr_lpa for r in ranges)
    if max_inr <= min_inr_lpa:
        return 2.0, f"INR {max_inr:g}L at threshold"
    # Scale 7–10 for INR above minimum up to ~60L
    bonus = min((max_inr - min_inr_lpa) / 30.0, 1.0) * 3.0
    return 7.0 + bonus, f"INR {max_inr:g}L"


def _profile_component(config: AppConfig, facts: ResumeFacts, jd: str, title: str) -> tuple[float, str]:
    if not jd or len(jd) < 80:
        return 3.0, "thin JD"

    skills = _match_skills(jd, config.profile.core_skills)
    skill_ratio = len(skills) / max(len(config.profile.core_skills), 1)
    skill_score = min(skill_ratio * 10.0, 10.0)

    roles = _pick_roles_for_jd(facts, jd, limit=3)
    role_hits = 0
    jd_lower = jd.lower()
    for role in roles:
        for highlight in role.get("highlights", []):
            for word in re.findall(r"[a-zA-Z]{4,}", highlight.lower()):
                if word in jd_lower:
                    role_hits += 1
    role_score = min(role_hits / 8.0 * 10.0, 10.0)

    title_lower = title.lower()
    headline_lower = facts.headline.lower()
    title_score = 5.0
    for token in ("backend", "senior", "python", "java", "platform", "api"):
        if token in title_lower and token in headline_lower:
            title_score += 1.0
    title_score = min(title_score, 10.0)

    combined = skill_score * 0.5 + role_score * 0.35 + title_score * 0.15
    reason = f"skills {len(skills)}/{len(config.profile.core_skills)}"
    if skills:
        reason += f" ({', '.join(skills[:4])})"
    return combined, reason


def score_job(config: AppConfig, job: T, *, facts: ResumeFacts | None = None) -> JobFitScore:
    facts = facts or load_resume_facts(config.base_dir)
    text = job_text(job)
    meta = dict(job.meta or {})
    min_lpa = config.application.min_inr_salary_lpa

    if not is_job_salary_eligible(jd=text, meta=meta, min_inr_lpa=min_lpa):
        reason = meta.get("salary_reason") or job_eligibility(jd=text, meta=meta, min_inr_lpa=min_lpa)[
            "salary_reason"
        ]
        return JobFitScore(
            total=0.0,
            salary=0.0,
            profile=0.0,
            eligible=False,
            reason=reason,
        )

    if meta.get("eligible_to_apply") is False and meta.get("location_blocked"):
        return JobFitScore(
            total=0.0,
            salary=0.0,
            profile=0.0,
            eligible=False,
            reason=meta.get("block_reason", "ineligible"),
        )

    salary_s, salary_reason = _salary_component(text, meta, min_inr_lpa=min_lpa)
    profile_s, profile_reason = _profile_component(config, facts, text, job.title)
    total = salary_s * 0.30 + profile_s * 0.70

    return JobFitScore(
        total=total,
        salary=salary_s,
        profile=profile_s,
        eligible=True,
        reason=f"salary {salary_reason}; {profile_reason}",
    )


def score_jobs_group(
    config: AppConfig,
    jobs: list[T],
    *,
    facts: ResumeFacts | None = None,
) -> list[tuple[T, JobFitScore]]:
    facts = facts or load_resume_facts(config.base_dir)
    return [(job, score_job(config, job, facts=facts)) for job in jobs]


def pick_best_per_company(
    config: AppConfig,
    jobs: list[T],
    *,
    facts: ResumeFacts | None = None,
) -> tuple[list[T], list[tuple[T, T, str]]]:
    """
    Return (winners, skipped) where skipped is (job, winner, reason).
    One opening per company — highest composite score wins.
    """
    if not jobs:
        return [], []

    if not config.application.one_job_per_company:
        eligible = [j for j in jobs if score_job(config, j, facts=facts).eligible]
        ineligible = [j for j in jobs if not score_job(config, j, facts=facts).eligible]
        skipped = [(j, j, "ineligible") for j in ineligible]
        return eligible, skipped

    facts = facts or load_resume_facts(config.base_dir)
    by_company: dict[str, list[T]] = {}
    for job in jobs:
        key = company_key(job.company)
        if not key:
            by_company.setdefault(f"__unknown_{job.job_id}", []).append(job)
            continue
        by_company.setdefault(key, []).append(job)

    winners: list[T] = []
    skipped: list[tuple[T, T, str]] = []

    for key, group in by_company.items():
        if len(group) == 1:
            fit = score_job(config, group[0], facts=facts)
            if fit.eligible:
                winners.append(group[0])
            else:
                skipped.append((group[0], group[0], fit.reason))
            continue

        scored = score_jobs_group(config, group, facts=facts)
        eligible = [(j, f) for j, f in scored if f.eligible]
        if not eligible:
            for job, fit in scored:
                skipped.append((job, job, fit.reason))
            continue

        eligible.sort(key=lambda x: x[1].total, reverse=True)
        winner, win_fit = eligible[0]
        winners.append(winner)
        logger.info(
            "Selected %s @ %s (score %.1f) — %s",
            winner.title,
            winner.company,
            win_fit.total,
            win_fit.reason,
        )
        for job, fit in eligible[1:]:
            reason = f"duplicate company; picked '{winner.title}' (score {win_fit.total:.1f} vs {fit.total:.1f})"
            skipped.append((job, winner, reason))
            logger.info("Skipped %s @ %s — %s", job.title, job.company, reason)

    return winners, skipped


def load_applied_companies(applied_jobs_path) -> set[str]:
    from pathlib import Path

    path = Path(applied_jobs_path)
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for entry in payload.get("history", []):
        company = str(entry.get("company", ""))
        ck = company_key(company)
        if ck:
            keys.add(ck)
    return keys
