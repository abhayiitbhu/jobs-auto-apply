"""Persisted, user-managed drop-keyword blocklist.

A small JSON file (``data/drop_keywords.json``) holding title keywords the user
has chosen to block — for example by replying ``drop python intern`` to a pending
question over Telegram. These are merged into ``profile.skip_role_keywords`` when
the config is loaded, so any future run skips jobs whose title matches them (same
whole-word, case-insensitive matching as the config-driven role filter).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppConfig

logger = logging.getLogger("job_apply")

_lock = threading.Lock()


def load_drop_keywords(config: AppConfig) -> list[str]:
    """Return the user's persisted drop keywords (de-duplicated, original order)."""
    path = config.drop_keywords_path
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("Could not read drop keywords from %s: %s", path, exc)
        return []
    raw = data.get("keywords", []) if isinstance(data, dict) else []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        kw = str(item or "").strip()
        if kw and kw.lower() not in seen:
            seen.add(kw.lower())
            out.append(kw)
    return out


def add_drop_keyword(config: AppConfig, keyword: str) -> bool:
    """Persist a new drop keyword. Returns True if it was added (False if blank/dupe)."""
    kw = (keyword or "").strip()
    if not kw:
        return False
    with _lock:
        existing = load_drop_keywords(config)
        if any(kw.lower() == e.lower() for e in existing):
            return False
        existing.append(kw)
        path = config.drop_keywords_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"keywords": existing}, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not save drop keyword %r to %s: %s", kw, path, exc)
            return False
    logger.info("Added drop keyword %r — future jobs with this in the title will be skipped.", kw)
    return True


def add_drop_keywords(config: AppConfig, keywords_text: str) -> tuple[list[str], list[str]]:
    """Persist one or more comma-separated drop keywords.

    Example: ``"data engineer, qa, intern"`` adds three separate keywords, each
    matched as its own whole-word/phrase title term. Returns ``(added, duplicates)``
    where ``added`` are the newly persisted keywords and ``duplicates`` were already
    present (blank entries are ignored).
    """
    added: list[str] = []
    duplicates: list[str] = []
    for raw in (keywords_text or "").split(","):
        kw = raw.strip()
        if not kw:
            continue
        if add_drop_keyword(config, kw):
            added.append(kw)
        elif kw.lower() not in {d.lower() for d in duplicates}:
            duplicates.append(kw)
    return added, duplicates
