"""Shared job-list filtering before apply."""

from __future__ import annotations

import logging

from .config import AppConfig
from .limits import apply_cap
from .role_filter import filter_no_experience_roles, filter_skipped_roles, role_filter_kwargs
from .run_issues import run_attempted_job_keys
from .utils import JobListing, filter_skipped_companies, job_key

logger = logging.getLogger("job_apply")


def _known_experience_skills(config: AppConfig) -> list[str]:
    """Skills the candidate has — core_skills plus skill_years entries above 0.

    Used as the keep-guard so a no-experience-skill title is only skipped when it
    does not also name a skill we actually have.
    """
    known = [s for s in config.profile.core_skills if str(s).strip()]
    try:
        from .profile.application_facts import load_application_facts

        app_facts = load_application_facts(config)
        skill_years = app_facts.get("skill_years")
        if isinstance(skill_years, dict):
            for name, years in skill_years.items():
                try:
                    if float(str(years).strip()) > 0:
                        known.append(str(name).replace("_", " "))
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass
    return known


def filter_pending_jobs(
    jobs: list[JobListing],
    applied_ids: set[str],
    limit: int,
    config: AppConfig,
) -> list[JobListing]:
    pending: list[JobListing] = []
    attempted_this_run = run_attempted_job_keys()
    skipped_attempted = 0
    skipped_applied = 0
    filtered = filter_skipped_roles(
        filter_skipped_companies(jobs, config.profile.skip_companies),
        **role_filter_kwargs(config.profile),
    )
    filtered = filter_no_experience_roles(
        filtered,
        no_exp_skills=config.profile.skip_no_experience_skills,
        known_skills=_known_experience_skills(config),
    )
    for job in filtered:
        key = job_key(job.source, job.job_id)
        if key in applied_ids:
            skipped_applied += 1
            continue
        if key in attempted_this_run:
            skipped_attempted += 1
            continue
        pending.append(job)
        cap = apply_cap(limit)
        if cap is not None and len(pending) >= cap:
            break
    if skipped_applied:
        logger.info(
            "Skipped %d job(s) already applied or deferred this run",
            skipped_applied,
        )
    if skipped_attempted:
        logger.info(
            "Skipped %d job(s) already attempted this run",
            skipped_attempted,
        )
    return pending
