from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig

# Reentrant so a mutate_memory body can call save_memory without self-deadlock.
_memory_lock = threading.RLock()
_DEFAULT_USER_MEMORY = "data/user_memory.json"
_memory_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def memory_path(base_dir: Path, config: AppConfig | None = None) -> Path:
    rel = config.paths.user_memory if config is not None else _DEFAULT_USER_MEMORY
    return base_dir / rel


def _memory_cache_key(path: Path) -> str:
    return str(path.resolve())


def _empty_memory() -> dict[str, Any]:
    return {"preferences": {}, "decisions": {}, "notes": {}, "question_answers": {}}


def _read_memory_from_disk(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_memory()
    return json.loads(path.read_text(encoding="utf-8"))


def _write_memory_to_disk(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    _memory_cache[_memory_cache_key(path)] = (mtime, data)


def load_memory(base_dir: Path, config: AppConfig | None = None) -> dict[str, Any]:
    path = memory_path(base_dir, config)
    cache_key = _memory_cache_key(path)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    cached = _memory_cache.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]
    data = _read_memory_from_disk(path)
    _memory_cache[cache_key] = (mtime, data)
    return data


def save_memory(base_dir: Path, data: dict[str, Any], config: AppConfig | None = None) -> None:
    path = memory_path(base_dir, config)
    with _memory_lock:
        _write_memory_to_disk(path, data)


def mutate_memory(
    base_dir: Path,
    mutate,
    config: AppConfig | None = None,
) -> None:
    """Atomically reload from disk, apply ``mutate(data)``, and persist.

    Reading fresh from disk *inside the lock* (rather than reusing a possibly
    stale cached/snapshot dict) prevents lost updates when several threads —
    parallel apply workers plus interactive manual saves — write concurrently.
    Without this, a worker holding a pre-edit snapshot would overwrite answers
    another thread just saved.
    """
    path = memory_path(base_dir, config)
    with _memory_lock:
        data = _read_memory_from_disk(path)
        mutate(data)
        _write_memory_to_disk(path, data)


def record_decision(
    base_dir: Path,
    *,
    job_key: str,
    status: str,
    platform: str,
    meta: dict[str, Any] | None = None,
) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data.setdefault("decisions", {})[job_key] = {
            "status": status,
            "platform": platform,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **(meta or {}),
        }

    mutate_memory(base_dir, _apply)


def get_decision(base_dir: Path, job_key: str) -> dict[str, Any] | None:
    return load_memory(base_dir).get("decisions", {}).get(job_key)


def save_preferences(base_dir: Path, preferences: dict[str, Any]) -> None:
    def _apply(data: dict[str, Any]) -> None:
        data["preferences"] = {**data.get("preferences", {}), **preferences}
        data["preferences"]["updated_at"] = datetime.now(timezone.utc).isoformat()

    mutate_memory(base_dir, _apply)
