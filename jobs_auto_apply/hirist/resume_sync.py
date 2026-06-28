from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from playwright.async_api import Page

from ..browser import hirist_session
from ..config import AppConfig
from .resume import ensure_resume_on_profile

logger = logging.getLogger("job_apply")


def _sync_interval(config: AppConfig) -> timedelta:
    return timedelta(minutes=config.resume.hirist_sync_interval_minutes)


def last_hirist_resume_sync_at(config: AppConfig) -> datetime | None:
    path = config.hirist_resume_sync_path
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        stamp = str(raw.get("last_sync_at", "")).strip()
        if not stamp:
            return None
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def record_hirist_resume_sync(config: AppConfig, when: datetime | None = None) -> None:
    stamp = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    path = config.hirist_resume_sync_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_sync_at": stamp}, indent=2) + "\n", encoding="utf-8")


def hirist_resume_sync_due(config: AppConfig) -> bool:
    last = last_hirist_resume_sync_at(config)
    if last is None:
        return True
    return datetime.now(timezone.utc) - last >= _sync_interval(config)


async def sync_hirist_resume_if_due(
    config: AppConfig,
    *,
    page: Page | None = None,
    force: bool = False,
) -> bool:
    """
    Upload resume to Hirist when sync_to_hirist is enabled and the last sync
    was more than hirist_sync_interval_minutes ago (or never). Returns True if upload ran.
    """
    if not config.resume.sync_to_hirist:
        return False
    interval_min = config.resume.hirist_sync_interval_minutes
    if not force and not hirist_resume_sync_due(config):
        last = last_hirist_resume_sync_at(config)
        logger.info(
            "Hirist resume sync skipped (last updated %s, within %s minutes)",
            last.isoformat() if last else "never",
            interval_min,
        )
        return False

    if page is not None:
        ok = await ensure_resume_on_profile(page, config.resume_path)
        if ok:
            record_hirist_resume_sync(config)
        return ok

    async with hirist_session(config) as (_, _context, session_page):
        ok = await ensure_resume_on_profile(session_page, config.resume_path)
        if ok:
            record_hirist_resume_sync(config)
        return ok


async def run_hirist_resume_sync_scheduler(
    config: AppConfig,
    stop: asyncio.Event,
) -> None:
    """Background loop: re-check on hirist_sync_interval_minutes while the run is active."""
    interval = _sync_interval(config)
    interval_sec = max(1, int(interval.total_seconds()))
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_sec)
            return
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            await sync_hirist_resume_if_due(config)
        except Exception as exc:
            logger.warning("Scheduled Hirist resume sync failed: %s", exc)
