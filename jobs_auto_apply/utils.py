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


_APPLIED_STATUSES = frozenset({"applied"})


def _load_applied_payload(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {"applied": [], "history": []}
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                logging.getLogger("job_apply").warning(
                    "Invalid %s — resetting to empty state", path.name
                )
                payload = {"applied": [], "history": []}
    if not isinstance(payload, dict):
        return {"applied": [], "history": []}
    payload.setdefault("applied", [])
    payload.setdefault("history", [])
    return payload


def _confirmed_job_keys_from_history(history: list[Any]) -> set[str]:
    """Only bot-confirmed successful applies block future runs."""
    last_entry: dict[str, dict[str, Any]] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("job_key", "")).strip()
        if not key:
            continue
        last_entry[key] = entry

    confirmed: set[str] = set()
    for key, entry in last_entry.items():
        raw_status = entry.get("status")
        if raw_status is None:
            continue
        if str(raw_status).strip() == "applied":
            confirmed.add(key)
    return confirmed


def _abandoned_job_keys_from_history(history: list[Any]) -> set[str]:
    """Jobs the user explicitly dismissed — never retry."""
    last_entry: dict[str, dict[str, Any]] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("job_key", "")).strip()
        if not key:
            continue
        last_entry[key] = entry

    abandoned: set[str] = set()
    for key, entry in last_entry.items():
        if str(entry.get("status", "")).strip() == "abandoned":
            abandoned.add(key)
    return abandoned


def _blocking_job_keys_from_history(history: list[Any]) -> set[str]:
    """Applied + in-run deferred jobs — used while a run is in progress."""
    last_entry: dict[str, dict[str, Any]] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("job_key", "")).strip()
        if not key:
            continue
        last_entry[key] = entry

    blocking: set[str] = set()
    for key, entry in last_entry.items():
        raw_status = entry.get("status")
        if raw_status is None:
            continue
        status = str(raw_status).strip()
        if status in ("applied", "deferred", "abandoned"):
            blocking.add(key)
    return blocking


def reconcile_applied_jobs(path: Path) -> int:
    """Rewrite applied[] from history; drop stale non-success history rows."""
    with _applied_jobs_lock:
        payload = _load_applied_payload(path)
        prev_applied = len(payload["applied"])
        prev_history = len(payload["history"])
        payload["history"] = [
            entry
            for entry in payload["history"]
            if isinstance(entry, dict)
            and str(entry.get("status", "")).strip() in ("applied", "abandoned")
        ]
        payload["applied"] = sorted(_confirmed_job_keys_from_history(payload["history"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        removed_applied = prev_applied - len(payload["applied"])
        removed_history = prev_history - len(payload["history"])
        log = logging.getLogger("job_apply")
        if removed_applied > 0:
            log.info(
                "Reconciled applied_jobs.json — removed %d stale key(s) from applied[]",
                removed_applied,
            )
        if removed_history > 0:
            log.info(
                "Pruned %d non-success row(s) from applied_jobs history",
                removed_history,
            )
        return removed_applied


def load_applied_jobs(path: Path, *, include_deferred: bool = True) -> set[str]:
    if not path.exists():
        return set()
    payload = _load_applied_payload(path)
    history = payload["history"]
    if history:
        if include_deferred:
            return _blocking_job_keys_from_history(history)
        return _confirmed_job_keys_from_history(history) | _abandoned_job_keys_from_history(
            history
        )
    # Legacy files without history — fall back to applied list.
    legacy = payload.get("applied") or []
    if isinstance(legacy, list):
        return {str(item) for item in legacy}
    return set()


_applied_jobs_lock = threading.Lock()


def save_applied_job(
    path: Path,
    key: str,
    meta: dict[str, Any],
    *,
    status: str = "applied",
) -> None:
    """Persist only a fully successful apply — partial attempts are not stored."""
    if status != "applied":
        return
    with _applied_jobs_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _load_applied_payload(path)

        entry = {
            "job_key": key,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            **meta,
        }
        payload["history"].append(entry)

        if status in _APPLIED_STATUSES:
            if key not in payload["applied"]:
                payload["applied"].append(key)
        elif key in payload["applied"]:
            payload["applied"].remove(key)

        payload["applied"] = sorted(_blocking_job_keys_from_history(payload["history"]))
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def record_deferred_apply(
    path: Path,
    key: str,
    meta: dict[str, Any],
    *,
    reason: str = "need answers",
) -> None:
    """Temporarily skip a job for the rest of this run (pending questions)."""
    with _applied_jobs_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _load_applied_payload(path)
        payload["history"].append(
            {
                "job_key": key,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "status": "deferred",
                "reason": reason,
                **meta,
            }
        )
        payload["applied"] = sorted(_blocking_job_keys_from_history(payload["history"]))
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def clear_deferred_applies(path: Path) -> int:
    """Remove in-run deferred rows after apply + pending-question flow finishes."""
    with _applied_jobs_lock:
        payload = _load_applied_payload(path)
        prev_history = len(payload["history"])
        payload["history"] = [
            entry
            for entry in payload["history"]
            if not (
                isinstance(entry, dict)
                and str(entry.get("status", "")).strip() == "deferred"
            )
        ]
        payload["applied"] = sorted(_blocking_job_keys_from_history(payload["history"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        removed = prev_history - len(payload["history"])
        if removed > 0:
            logging.getLogger("job_apply").info(
                "Cleared %d deferred job(s) from applied_jobs.json",
                removed,
            )
        return removed


def defer_job_for_run(
    path: Path,
    job: JobListing,
    *,
    reason: str = "need answers",
) -> None:
    record_deferred_apply(
        path,
        job_key(job.source, job.job_id),
        {
            "source": job.source,
            "title": job.title,
            "url": job.url,
        },
        reason=reason,
    )


def record_abandoned_apply(
    path: Path,
    key: str,
    meta: dict[str, Any],
    *,
    reason: str = "user dismissed",
) -> None:
    """Permanently skip a job the user chose not to pursue."""
    with _applied_jobs_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _load_applied_payload(path)
        payload["history"].append(
            {
                "job_key": key,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "status": "abandoned",
                "reason": reason,
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

