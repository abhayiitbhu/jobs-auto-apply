#!/usr/bin/env python3
"""Broad audit and repair of data/user_memory.json question_answers."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobs_auto_apply.answers.config_answers import (  # noqa: E402
    compensation_answer,
    location_answer,
    skill_years_config_answer,
)
from jobs_auto_apply.application_questions import (  # noqa: E402
    answer_acceptable_for_field,
    enrich_field_for_llm,
    is_llm_meta_answer,
    is_placeholder_answer,
    is_skill_years_question,
    needs_review_answer,
)
from jobs_auto_apply.config import load_config  # noqa: E402
from jobs_auto_apply.question_groups import classify_question  # noqa: E402
from jobs_auto_apply.rag_answers import generate_rag_answer  # noqa: E402

MEMORY_PATH = ROOT / "data" / "user_memory.json"

# Sources that come from you directly; their answers are authoritative and must
# never be auto-flagged for review by the context-free acceptance heuristic.
_HUMAN_SOURCES = {"manual", "confirmed", "interactive", "pending", "reviewed"}

# Heuristic markers of a resume / cover-letter prose dump that was accidentally
# stored as a short-field answer. These signals virtually never appear in a
# legitimate short answer, so matching one means the entry is junk. Kept generic
# (no personal data) so it works for any user's memory file.
_RESUME_DUMP = re.compile(
    r"results[- ]driven"
    r"|proven track record"
    r"|(?:implemented|spearheaded|architected|orchestrated|engineered)\s+\w+"
    r"|ci/cd pipelines?"
    r"|[\w.+-]+@[\w-]+\.[\w.-]+"  # email address
    r"|\bcurriculum vitae\b",
    re.I,
)


def _is_yesno_question(question: str) -> bool:
    q = question.lower()
    if is_skill_years_question(q):
        return False
    if re.search(r"\b(how many|years of|notice|ctc|salary|pincode|pin code|lwd)\b", q):
        return False
    if re.search(r"\bdo you have\b.+\b(years?|yrs)\b", q):
        return False
    return bool(
        re.search(
            r"\b(are you|do you|have you|will you|can you|willing|comfortable|"
            r"available|open to|legally|sponsorship|permitted|ok with|intrested|"
            r"interested|experience in)\b",
            q,
        )
    )


def _normalize_yesno(answer: str) -> str | None:
    a = answer.strip()
    if re.fullmatch(r"yes", a, re.I):
        return "Yes"
    if re.fullmatch(r"no", a, re.I):
        return "No"
    return None


def _rag_short_answer(config, question: str, field: dict) -> str | None:
    rag = generate_rag_answer(config, question=question, field=field)
    if not rag:
        return None
    text = rag.strip()
    if len(text) > 120 or _RESUME_DUMP.search(text):
        return None
    if not answer_acceptable_for_field(question, text, field):
        return None
    return text


def _canonical_answer(config, question: str, field: dict) -> tuple[str | None, str]:
    gid = classify_question(question)

    if gid == "compensation":
        val = compensation_answer(config, question, field)
        if val and answer_acceptable_for_field(question, val, field):
            return val, "config_ctc"

    if gid in ("current_location", "preferred_location"):
        val = location_answer(config, question)
        if val and answer_acceptable_for_field(question, val, field):
            return val, "config_loc"

    if gid.startswith("skill:") and is_skill_years_question(question):
        if question in _MANUAL or (_is_yesno_question(question) and not re.search(r"\bhow many\b", question, re.I)):
            pass
        else:
            val = skill_years_config_answer(config, question)
            if val is not None and answer_acceptable_for_field(question, val, field):
                return val, "config_skill"

    if gid in ("notice_period", "f2f_interview", "pan", "uan", "pincode", "total_experience"):
        val = _rag_short_answer(config, question, field)
        if val:
            return val, "rag"

    if gid.startswith("skill_yesno:") or (gid.startswith("unique:") and _is_yesno_question(question)):
        val = _rag_short_answer(config, question, field)
        if val and re.match(r"^(yes|no)\b", val, re.I):
            return _normalize_yesno(val) or val, "rag"

    return None, ""


def _should_overwrite(question: str, old: str, new: str, gid: str) -> bool:
    if old.strip() == new.strip():
        return False
    if is_placeholder_answer(old) or is_llm_meta_answer(old) or _RESUME_DUMP.search(old):
        return True
    if needs_review_answer(question, old):
        return True
    if old.strip().lower() in ("all", "none") and gid.startswith("unique:"):
        return True
    if re.fullmatch(r"\d+\s*days?", old.strip(), re.I) and gid not in (
        "notice_period",
        "join_availability",
    ):
        return True
    if gid.startswith("skill:") and is_skill_years_question(question):
        return True
    if gid in ("notice_period", "f2f_interview", "compensation"):
        return True
    return bool(gid.startswith("skill_yesno:"))


# Manual corrections for entries the classifier mishandles.
_MANUAL: dict[str, str] = {
    "How much experience do you have with Salesforce Marketing Cloud?": "0",
    "What is your exp on Backend Development using JAVA": "4",
    "Which tool you have experience with (ML framework)?": "0",
    "Which tool you have experience with (MLops)?": "0",
    "How many years of experience do you have in Pyspark?": "0",
    "Do You have 5+ Years of Experience in Data Engineering Domain ?": "No",
    "Do you have 5+ years of experience in Python backend development?": "No",
    "Experience in Insurance / Claims / Fraud Detection?": "No",
    "Experience in Machine Learning & Deep Learning?": "No",
    "Do you have experience in Aws Lambda, Glue and Redshift?": "No",
    "What is your notice period? and when is your LWD?": "12/06/2026",
    "What are your total years of experience into Snowflake and Microsoft Data Fabric?": "0",
}


def _is_known_good(question: str, answer: str, gid: str) -> bool:
    a = answer.strip()
    if gid == "notice_period" and a in ("0", "Immediately available"):
        return True
    if gid == "f2f_interview" and a in ("Yes", "No"):
        return True
    if _is_yesno_question(question) and a in ("Yes", "No"):
        return True
    return bool(question in _MANUAL and a == _MANUAL[question])


def main() -> None:
    config = load_config(ROOT / "config.yaml")
    data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
    answers = data.get("question_answers", {})

    stats: Counter[str] = Counter()
    changes: list[tuple[str, str, str, str, str]] = []
    remaining_issues: list[tuple[str, str, str]] = []

    for key, entry in list(answers.items()):
        question = str(entry.get("question", "")).strip()
        old = str(entry.get("answer", "")).strip()
        if not question:
            continue

        field = enrich_field_for_llm({"kind": "text", "label": question})
        gid = classify_question(question)

        norm = _normalize_yesno(old)
        if norm and _is_yesno_question(question) and norm != old:
            entry["answer"] = norm
            entry["reviewed"] = True
            entry["source"] = "cleanup"
            stats["normalize_yesno"] += 1
            changes.append((gid, question[:60], old, norm, "normalize_yesno"))
            old = norm

        canonical, source = _canonical_answer(config, question, field)
        if canonical and _should_overwrite(question, old, canonical, gid):
            entry["answer"] = canonical
            entry["reviewed"] = True
            entry["source"] = source
            entry.pop("needs_review", None)
            stats[source] += 1
            changes.append((gid, question[:60], old, canonical, source))
            old = canonical

        # Manual overrides win over auto layers (classifier edge cases).
        manual = _MANUAL.get(question)
        if manual and manual != old:
            entry["answer"] = manual
            entry["reviewed"] = True
            entry["source"] = "manual"
            entry.pop("needs_review", None)
            stats["manual"] += 1
            changes.append((gid, question[:60], old, manual, "manual"))
            old = manual

        if _RESUME_DUMP.search(old) or is_llm_meta_answer(old):
            entry.pop("reviewed", None)
            entry["needs_review"] = True
            stats["prose_flagged"] += 1
            continue

        if _is_known_good(question, old, gid):
            entry["reviewed"] = True
            entry.pop("needs_review", None)
            continue

        # Respect human-authored / reviewed answers. An answer you typed
        # (source=manual/confirmed/interactive/pending) or already reviewed is
        # authoritative — the context-free acceptance heuristic here lacks the
        # field's real options/type and false-positives on valid answers
        # ("AWS", "84.4", "4", "45"). Garbage (placeholder / meta / resume dump)
        # was already filtered above, so such an answer is kept (and its review
        # restored) instead of being re-flagged every run.
        if (
            entry.get("reviewed") or str(entry.get("source", "")).lower() in _HUMAN_SOURCES
        ) and not is_placeholder_answer(old):
            entry["reviewed"] = True
            entry.pop("needs_review", None)
            continue

        if not answer_acceptable_for_field(question, old, field):
            remaining_issues.append((gid, question[:60], old))
            entry.pop("reviewed", None)
            entry["needs_review"] = True
            stats["flagged"] += 1
        elif entry.get("needs_review"):
            entry.pop("needs_review", None)
            if not entry.get("reviewed"):
                entry["reviewed"] = True

    # Restore common notice-period keys if missing (correct per application_facts)
    notice_defaults = {
        "What is your notice period?": "0",
        "What is your notice period in days ?": "0",
        "What is your notice period as per your Offer Letter ?": "0",
    }
    from jobs_auto_apply.question_keys import question_key

    for q, ans in notice_defaults.items():
        qk = question_key(q)
        if qk not in answers:
            answers[qk] = {
                "question": q,
                "answer": ans,
                "reviewed": True,
                "source": "config",
            }
            stats["restored_notice"] += 1
            changes.append(("notice_period", q[:60], "", ans, "restored_notice"))

    MEMORY_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Entries: {len(answers)}")
    print("\nChanges by type:")
    for kind, count in stats.most_common():
        print(f"  {kind}: {count}")

    print(f"\nSample fixes ({min(20, len(changes))} of {len(changes)}):")
    for row in changes[:20]:
        print(f"  [{row[4]}] {row[1]}")
        print(f"    {row[2]!r} -> {row[3]!r}")

    if remaining_issues:
        print(f"\nStill flagged needs_review ({len(remaining_issues)}):")
        for gid, q, a in remaining_issues[:20]:
            print(f"  [{gid}] {q} = {a!r}")

    needs = sum(1 for e in answers.values() if needs_review_answer(e.get("question", ""), e.get("answer", "")))
    bad = sum(
        1
        for e in answers.values()
        if not answer_acceptable_for_field(
            e.get("question", ""),
            e.get("answer", ""),
            enrich_field_for_llm({"kind": "text", "label": e.get("question", "")}),
        )
    )
    prose = sum(1 for e in answers.values() if _RESUME_DUMP.search(str(e.get("answer", ""))))
    print(f"\nPost-cleanup: needs_review={needs}, unacceptable={bad}, resume_prose={prose}, entries={len(answers)}")


if __name__ == "__main__":
    main()
