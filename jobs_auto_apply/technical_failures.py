"""Persistent log of jobs that failed to apply for *technical* reasons.

This is distinct from "need answers" skips (a content problem the user can
resolve by answering a question). Technical failures are DOM/automation issues
worth reviewing or retrying later, e.g.:
  - a known answer that could not be clicked/filled (radio/chip/Next disabled)
  - the chatbot/form could not be completed
  - page/browser timeouts or crashes

Entries accumulate across runs (deduped by job key) so the list is maintained
over time. File: data/technical_failures.json
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("job_apply")

_lock = threading.Lock()


def technical_failures_path(base_dir: Path) -> Path:
    return base_dir / "data" / "technical_failures.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"failures": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("failures"), dict):
            return data
    except Exception:
        pass
    return {"failures": {}}


def record_technical_failure(
    base_dir: Path,
    *,
    job_key: str,
    source: str,
    title: str,
    company: str = "",
    url: str = "",
    reason: str,
) -> None:
    """Append/update a technical apply failure, deduped by job_key."""
    if not job_key:
        return
    path = technical_failures_path(base_dir)
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        data = _load(path)
        failures = data["failures"]
        entry = failures.get(job_key)
        if entry is None:
            failures[job_key] = {
                "source": source,
                "title": title,
                "company": company,
                "url": url,
                "reason": reason,
                "first_seen": now,
                "last_seen": now,
                "count": 1,
            }
        else:
            entry["reason"] = reason
            entry["last_seen"] = now
            entry["count"] = int(entry.get("count", 0)) + 1
            if company and not entry.get("company"):
                entry["company"] = company
            if url and not entry.get("url"):
                entry["url"] = url
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    logger.info(
        "Recorded technical apply failure [%s]: %s%s (%s)",
        source,
        title[:60],
        f" @ {company}" if company else "",
        reason,
    )


def clear_technical_failure(base_dir: Path, job_key: str) -> bool:
    """Remove a job from the technical-failures log (e.g. once it is applied).

    Returns True if an entry was removed. Used so a job that later succeeds or is
    found already-applied stops lingering as a stale "failure".
    """
    if not job_key:
        return False
    path = technical_failures_path(base_dir)
    with _lock:
        data = _load(path)
        if job_key not in data["failures"]:
            return False
        data["failures"].pop(job_key, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    logger.info("Cleared technical failure for %s (now applied/resolved)", job_key)
    return True


def _normalize_url(url: str) -> str:
    """Strip query/fragment/trailing slash so the same job compares equal."""
    url = (url or "").strip()
    if not url:
        return ""
    url = url.split("?", 1)[0].split("#", 1)[0]
    return url.rstrip("/").lower()


def matching_failures(
    base_dir: Path,
    *,
    source: str = "",
    url: str = "",
    job_id: str = "",
) -> dict[str, dict[str, Any]]:
    """Return ``{key: entry}`` for every failure that refers to the same job.

    A single job can be logged under more than one key (e.g. both
    ``naukri:<numeric id>`` and ``naukri:job-listings-…<numeric id>``), so match
    by normalized URL, by the job id embedded in the URL, and by exact key.
    """
    target_url = _normalize_url(url)
    failures = _load(technical_failures_path(base_dir))["failures"]
    out: dict[str, dict[str, Any]] = {}
    for key, entry in failures.items():
        if source and entry.get("source") != source:
            continue
        entry_url = _normalize_url(str(entry.get("url", "")))
        matched = (
            (bool(target_url) and entry_url == target_url)
            or (bool(job_id) and bool(source) and key == f"{source}:{job_id}")
            or (bool(job_id) and job_id in str(entry.get("url", "")))
        )
        if matched:
            out[key] = entry
    return out


def clear_technical_failures_for_job(
    base_dir: Path,
    *,
    source: str = "",
    url: str = "",
    job_id: str = "",
) -> list[str]:
    """Remove every failure entry referring to a job that is no longer failing.

    Unlike :func:`clear_technical_failure` (exact key only), this clears all key
    variants for the same job. Returns the list of removed keys.
    """
    path = technical_failures_path(base_dir)
    with _lock:
        data = _load(path)
        failures = data["failures"]
        target_url = _normalize_url(url)
        removed = [
            key
            for key, entry in list(failures.items())
            if (not source or entry.get("source") == source)
            and (
                (bool(target_url) and _normalize_url(str(entry.get("url", ""))) == target_url)
                or (bool(job_id) and bool(source) and key == f"{source}:{job_id}")
                or (bool(job_id) and job_id in str(entry.get("url", "")))
            )
        ]
        if not removed:
            return []
        for key in removed:
            failures.pop(key, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    logger.info(
        "Cleared %d technical failure(s) for resolved job: %s",
        len(removed),
        ", ".join(removed),
    )
    return removed


def load_technical_failures(base_dir: Path) -> list[dict[str, Any]]:
    items = list(_load(technical_failures_path(base_dir))["failures"].values())
    items.sort(key=lambda e: str(e.get("last_seen", "")), reverse=True)
    return items


def technical_failure_count(base_dir: Path) -> int:
    return len(_load(technical_failures_path(base_dir))["failures"])


def technical_failures_summary(base_dir: Path) -> str:
    items = load_technical_failures(base_dir)
    if not items:
        return ""
    lines = [f"{len(items)} job(s) have technical apply failures (data/technical_failures.json):"]
    for it in items[:12]:
        where = str(it.get("title", ""))
        if it.get("company"):
            where += f" @ {it['company']}"
        count = it.get("count", 1)
        repeat = f" x{count}" if count and int(count) > 1 else ""
        lines.append(f"  • [{it.get('source')}] {where} — {it.get('reason')}{repeat}")
    if len(items) > 12:
        lines.append(f"  … and {len(items) - 12} more")
    return "\n".join(lines)
