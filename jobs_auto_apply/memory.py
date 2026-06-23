from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def memory_path(base_dir: Path) -> Path:
    return base_dir / "data" / "user_memory.json"


def load_memory(base_dir: Path) -> dict[str, Any]:
    path = memory_path(base_dir)
    if not path.exists():
        return {"preferences": {}, "decisions": {}, "notes": {}, "question_answers": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_memory(base_dir: Path, data: dict[str, Any]) -> None:
    path = memory_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def record_decision(
    base_dir: Path,
    *,
    job_key: str,
    status: str,
    platform: str,
    meta: dict[str, Any] | None = None,
) -> None:
    data = load_memory(base_dir)
    data.setdefault("decisions", {})[job_key] = {
        "status": status,
        "platform": platform,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **(meta or {}),
    }
    save_memory(base_dir, data)


def get_decision(base_dir: Path, job_key: str) -> dict[str, Any] | None:
    return load_memory(base_dir).get("decisions", {}).get(job_key)


def save_preferences(base_dir: Path, preferences: dict[str, Any]) -> None:
    data = load_memory(base_dir)
    data["preferences"] = {**data.get("preferences", {}), **preferences}
    data["preferences"]["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_memory(base_dir, data)
