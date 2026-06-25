"""Migrate data/user_memory.json -> group-keyed, reconciled answers.

Collapses per-phrasing duplicate answers into one canonical entry per
``memory_key`` (e.g. all AI/ML phrasings -> ``skill:ai_ml``), resolving
conflicts by source trust -> reviewed -> recency. Config/facts answers that can
be re-derived from application_facts.yaml are dropped (kept ephemeral).

Dry-run by default (prints stats only). Pass --apply to write, which first
creates a timestamped backup next to the file.

    python scripts/migrate_memory_to_groups.py            # dry run
    python scripts/migrate_memory_to_groups.py --apply    # write changes
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobs_auto_apply.answers.config_answers import authoritative_config_answer  # noqa: E402
from jobs_auto_apply.answers.memory_store import _source_rank, memory_key  # noqa: E402
from jobs_auto_apply.answers.validation import is_placeholder_answer  # noqa: E402
from jobs_auto_apply.config import load_config  # noqa: E402

MEMORY_PATH = ROOT / "data" / "user_memory.json"


def _recency(entry: dict) -> str:
    return str(entry.get("updated_at", "") or "")


def _win_key(entry: dict) -> tuple:
    return (
        _source_rank(entry.get("source")),
        1 if entry.get("reviewed") else 0,
        0 if entry.get("needs_review") else 1,
        _recency(entry),
    )


def _reconcile(entries: list[dict]) -> dict:
    """Pick the strongest entry, merging examples + hits from the rest."""
    winner = max(entries, key=_win_key)
    merged = dict(winner)
    examples: list[str] = []
    hits = 0
    for e in entries:
        hits += int(e.get("hits", 0) or 0) or 1
        for ex in [str(e.get("question", "")), *(e.get("examples", []) or [])]:
            ex = str(ex).strip()
            if ex and ex not in examples:
                examples.append(ex)
    merged["examples"] = examples[-10:]
    merged["hits"] = hits
    merged.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    return merged


def main() -> None:
    apply = "--apply" in sys.argv
    config = load_config(MEMORY_PATH.parent.parent / "config.yaml")

    data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    answers = data.get("question_answers", {})

    buckets: dict[str, list[dict]] = {}
    no_question = 0
    for entry in answers.values():
        if not isinstance(entry, dict):
            continue
        q = str(entry.get("question", "")).strip()
        if not q:
            no_question += 1
            continue
        buckets.setdefault(memory_key(q), []).append(entry)

    new_answers: dict[str, dict] = {}
    dropped_derivable = 0
    collapsed = 0
    for key, entries in buckets.items():
        merged = _reconcile(entries)
        if len(entries) > 1:
            collapsed += len(entries) - 1
        # If YAML/config can authoritatively answer this question, drop the memory
        # entry entirely — YAML is the single source of truth and noisy legacy
        # values (e.g. python='0', ci_cd='4') must not shadow it.
        q = str(merged.get("question", ""))
        auth = authoritative_config_answer(config, q, None)
        if auth and not is_placeholder_answer(auth):
            dropped_derivable += 1
            continue
        new_answers[key] = merged

    print(f"Entries before:           {len(answers)}")
    print(f"  (no question field):     {no_question}")
    print(f"Canonical keys after:     {len(new_answers)}")
    print(f"Duplicates collapsed:     {collapsed}")
    print(f"YAML-derivable dropped:   {dropped_derivable} (re-derived from facts each run)")

    # Show a few large collapses for sanity.
    big = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)[:12]
    print("\nLargest collapses (key -> #entries, distinct answers):")
    for key, entries in big:
        if len(entries) < 2:
            continue
        vals = sorted({str(e.get("answer", "")).strip() for e in entries})
        win = _reconcile(entries).get("answer", "")
        print(f"  {key:32} x{len(entries):<3} winner={win!r:12} from {vals}")

    if not apply:
        print("\nDRY RUN — no changes written. Re-run with --apply to write.")
        return

    backup = MEMORY_PATH.with_suffix(
        f".backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    )
    shutil.copy2(MEMORY_PATH, backup)
    data["question_answers"] = new_answers
    MEMORY_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"\nWROTE {MEMORY_PATH} (backup at {backup.name})")


if __name__ == "__main__":
    main()
