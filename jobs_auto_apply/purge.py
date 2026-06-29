"""Periodically wipe accumulated run state so the bot starts fresh.

Every ``state.purge_interval_days`` (default 7) this clears the transient state
that piles up across runs:

  - the run log (``data/run.log``)
  - the applied-jobs list + history (``data/applied_jobs.json``)
  - pending questions (``data/pending_questions.json``)

Purging applied_jobs.json is intentional: after a purge the bot will re-apply
to jobs it previously applied to. User-curated / learned state is deliberately
left untouched — login sessions, ``user_memory.json``, the drop-keyword
blocklist, Telegram/WhatsApp state, the FAISS index and review queue all
survive a purge.

The last-purge timestamp is persisted to ``data/purge_state.json`` so the cycle
is based on wall-clock time and survives ``serve`` restarts. On the very first
run (no state file yet) we record "now" and do not purge, so the first purge
happens one full interval after the bot is first started.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppConfig

logger = logging.getLogger("job_apply")


def _read_last_purged_at(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("last_purged_at") if isinstance(data, dict) else None
        if not raw:
            return None
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _write_last_purged_at(path: Path, when: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_purged_at": when.isoformat()}, indent=2),
        encoding="utf-8",
    )


def _truncate_log(log_path: Path) -> None:
    """Empty the run log, truncating the live handler stream in place if open.

    The named ``job_apply`` logger keeps the log file open in append mode, so we
    truncate through that handler when possible to avoid leaving a dangling file
    descriptor pointed at an unlinked inode.
    """
    truncated_via_handler = False
    for handler in list(logging.getLogger("job_apply").handlers):
        stream = getattr(handler, "stream", None)
        base = getattr(handler, "baseFilename", None)
        if stream is None or base is None:
            continue
        try:
            if Path(base) != log_path:
                continue
        except Exception:
            continue
        try:
            handler.acquire()
            stream.flush()
            stream.truncate(0)
            stream.seek(0)
            truncated_via_handler = True
        except Exception:
            pass
        finally:
            handler.release()
    if not truncated_via_handler and log_path.exists():
        try:
            log_path.write_text("", encoding="utf-8")
        except Exception:
            pass


def purge_now(config: AppConfig) -> list[str]:
    """Wipe transient run state immediately. Returns the names that were cleared."""
    cleared: list[str] = []

    _truncate_log(config.log_path)
    cleared.append(config.log_path.name)

    delete_paths = [
        config.applied_jobs_path,
        config.pending_questions_path,
    ]
    for path in delete_paths:
        try:
            if path.exists():
                path.unlink()
                cleared.append(path.name)
        except Exception:
            logger.warning("Purge: could not remove %s", path, exc_info=True)

    return cleared


def maybe_purge(config: AppConfig) -> bool:
    """Purge run state if a full interval has elapsed since the last purge.

    Returns True if a purge was performed this call.
    """
    if not config.state.purge_enabled:
        return False
    interval_days = config.state.purge_interval_days
    if interval_days <= 0:
        return False

    state_path = config.purge_state_path
    now = datetime.now(timezone.utc)
    last = _read_last_purged_at(state_path)

    if last is None:
        # First time we've seen this install — start the clock, don't purge yet.
        _write_last_purged_at(state_path, now)
        return False

    if now - last < timedelta(days=interval_days):
        return False

    logger.info(
        "Purge: %d day(s) elapsed since last purge (%s) — clearing run state.",
        interval_days,
        last.isoformat(),
    )
    cleared = purge_now(config)
    _write_last_purged_at(state_path, now)
    logger.info("Purge: cleared %s", ", ".join(cleared) if cleared else "(nothing)")
    return True
