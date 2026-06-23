from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class JobListing:
    job_id: str
    title: str
    company: str
    url: str
    source: str  # wellfound | uplers
    easy_apply: bool = False
    external_ats: bool = False
    external_url: str | None = None
    description: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def setup_logging(log_path: Path, verbose: bool = False) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("job_apply")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


def job_key(source: str, job_id: str) -> str:
    return f"{source}:{job_id}"


def load_applied_jobs(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {str(item) for item in data}
    if isinstance(data, dict) and "applied" in data:
        return {str(item) for item in data["applied"]}
    return set()


_applied_jobs_lock = threading.Lock()


def save_applied_job(path: Path, key: str, meta: dict[str, Any]) -> None:
    with _applied_jobs_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = {"applied": [], "history": []}

        if key not in payload["applied"]:
            payload["applied"].append(key)
        payload["history"].append(
            {
                "job_key": key,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                **meta,
            }
        )
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def render_cover_note(template: str, *, title: str, company: str) -> str:
    """Legacy static template helper."""
    return (
        template.replace("{{title}}", title)
        .replace("{{company}}", company)
        .replace("{{job_title}}", title)
    )


def _normalize_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def company_key(company: str) -> str:
    """Normalized company name for deduplication."""
    return _normalize_company(company)


def should_skip_company(company: str, skip_list: list[str]) -> bool:
    """Return True if job company matches any skip entry (substring, case-insensitive)."""
    if not company or not skip_list:
        return False
    normalized = _normalize_company(company)
    if not normalized:
        return False
    for skip in skip_list:
        needle = _normalize_company(skip)
        if needle and (needle in normalized or normalized in needle):
            return True
    return False


def filter_skipped_companies(jobs: list[JobListing], skip_list: list[str]) -> list[JobListing]:
    if not skip_list:
        return jobs
    logger = logging.getLogger("job_apply")
    kept: list[JobListing] = []
    for job in jobs:
        if should_skip_company(job.company, skip_list):
            logger.info("Skipping company: %s (%s)", job.company, job.title)
            continue
        kept.append(job)
    return kept

