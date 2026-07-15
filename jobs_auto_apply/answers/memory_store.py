from __future__ import annotations

import logging
import re
from typing import Any

from ..answer_suggest import is_employer_check_question, is_prior_application_screening
from ..config import AppConfig
from ..memory import load_memory, mutate_memory
from ..question_keys import question_key
from .chip_options import is_lpa_chip_option, is_notice_chip_option, pick_lpa_chip_option
from .chips import (
    _YEAR_EXPERIENCE_Q,
    _match_years_to_chip_option,
    _normalize_to_option,
    _value_in_answer_range,
    parse_years_numeric_value,
    pick_notice_period_option,
)
from .compensation import resolve_ctc_numeric_answer
from .config_answers import ctc_want_kind
from .experience import is_skill_years_question
from .fields import (
    enrich_field_for_llm,
    infer_field_input_type,
    is_last_working_day_question,
    is_numeric_ctc_question,
)
from .labels import normalize_question_label
from .location import (
    is_location_value_question,
    is_relocation_yesno_question,
    map_city_to_location_chip,
    saved_location_answer_matches_question,
)
from .validation import (
    answer_usable,
    is_llm_meta_answer,
    needs_review_answer,
    normalize_employer_radio_answer,
)

logger = logging.getLogger("job_apply")

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def sanitize_user_answer(text: str) -> str:
    """Strip terminal cursor-control sequences accidentally pasted into answers."""
    return _ANSI_ESCAPE_RE.sub("", text).strip()


def canonicalize_stored_answer(
    question: str,
    answer: str,
    field: dict[str, Any] | None = None,
    config: AppConfig | None = None,
) -> str:
    """Store specific values (e.g. '4') instead of chip labels (e.g. '<6 years')."""
    text = sanitize_user_answer(answer)
    if not text:
        return text

    if is_employer_check_question(question):
        normalized = normalize_employer_radio_answer(question, text)
        if normalized in ("Yes", "No"):
            return normalized

    if is_prior_application_screening(question):
        normalized = normalize_employer_radio_answer(question, text)
        if normalized in ("Yes", "No"):
            return normalized

    label = question.lower()
    options = [str(o).strip() for o in (field or {}).get("options", []) if str(o).strip()]

    plain_num = re.fullmatch(r"(\d+)(?:\s*years?)?", text, re.I)
    if plain_num:
        return plain_num.group(1)

    if _YEAR_EXPERIENCE_Q.search(label) or (
        options and any(re.search(r"<\s*\d+|\d+\s*[-–]\s*\d+|\d+\s*\+", o) for o in options)
    ):
        num = re.search(r"(\d+)", text)
        if num and not re.search(r"<\s*\d+|\d+\s*[-–]\s*\d+", text):
            return num.group(1)
        if config and config.profile.years_experience:
            for opt in options:
                if _value_in_answer_range(config.profile.years_experience, opt):
                    return str(config.profile.years_experience)
            return str(config.profile.years_experience)

    if options and {o.lower() for o in options} <= {"yes", "no"}:
        if re.search(r"\byes\b", text, re.I):
            return "Yes"
        if re.search(r"\bno\b", text, re.I):
            return "No"

    if is_numeric_ctc_question(question):
        resolved = resolve_ctc_numeric_answer(question, text, config)
        if resolved and re.fullmatch(r"\d+(?:\.\d+)?", resolved):
            return resolved

    return text


def resolve_fill_answer(
    stored: str,
    field: dict[str, Any],
    config: AppConfig | None = None,
) -> str:
    """Map canonical stored value to the value needed to fill the form."""
    text = stored.strip()
    if not text:
        return text

    label = str(field.get("label", ""))
    if is_last_working_day_question(label):
        from .config_answers import facts_serving_notice

        if not facts_serving_notice(config) and re.search(
            r"\belse\b|\bn/?a\b|\bright\s+na\b",
            label,
            re.I,
        ):
            return "NA"

    if is_location_value_question(label) and re.fullmatch(r"\d+(?:\.\d+)?", text):
        return ""

    if is_employer_check_question(label):
        normalized = normalize_employer_radio_answer(label, text)
        if normalized in ("Yes", "No"):
            return normalized

    if is_prior_application_screening(label):
        normalized = normalize_employer_radio_answer(label, text)
        if normalized in ("Yes", "No"):
            return normalized

    kind = str(field.get("kind", "text"))
    options = [str(o).strip() for o in field.get("options", []) if str(o).strip()]

    if is_numeric_ctc_question(label):
        resolved = resolve_ctc_numeric_answer(label, text, config)
        if resolved:
            want = ctc_want_kind(label)
            if want == "both" and "/" in resolved:
                return resolved
            if re.fullmatch(r"\d+(?:\.\d+)?", resolved):
                return resolved

    input_type = infer_field_input_type(label, field)
    if input_type == "ctc_numeric":
        resolved = resolve_ctc_numeric_answer(label, text, config)
        if resolved:
            want = ctc_want_kind(label)
            if want == "both" and "/" in resolved:
                return resolved
            if re.fullmatch(r"\d+(?:\.\d+)?", resolved):
                return resolved

    if kind in ("radio", "checkbox_group") and options:
        lpa_opts = [o for o in options if is_lpa_chip_option(o)]
        if lpa_opts and (is_numeric_ctc_question(label) or input_type == "ctc_numeric"):
            resolved = resolve_ctc_numeric_answer(label, text, config)
            numeric: float | None = None
            if resolved and re.fullmatch(r"\d+(?:\.\d+)?", resolved):
                numeric = float(resolved)
            else:
                num_m = re.search(r"(\d+(?:\.\d+)?)", text)
                if num_m:
                    numeric = float(num_m.group(1))
            if numeric is not None:
                chip = pick_lpa_chip_option(numeric, lpa_opts)
                if chip:
                    return chip

        notice_opts = [o for o in options if is_notice_chip_option(o)]
        is_notice_q = bool(
            re.search(r"\bnotice period\b", label, re.I)
            or re.search(r"\bnp\b", label, re.I)
            or input_type == "notice_period"
        )
        if notice_opts and (is_notice_q or (is_numeric_ctc_question(label) and not lpa_opts)):
            notice = pick_notice_period_option(text, notice_opts)
            if notice:
                return notice

        if re.search(r"\bnotice period\b", label, re.I):
            notice = pick_notice_period_option(text, options)
            if notice:
                return notice
        if kind == "checkbox_group" and re.search(r"\b(how soon|available to join|join)\b", label, re.I):
            if re.search(r"\b(yes|immediate|immediately|available)\b", text, re.I):
                for opt in options:
                    if re.search(r"\bimmediate", opt, re.I):
                        return opt.strip()
        location_chip = map_city_to_location_chip(text, options)
        if location_chip:
            return location_chip
        years_field = is_skill_years_question(label) or infer_field_input_type(label, field) == "years_numeric"
        if years_field:
            years = parse_years_numeric_value(text)
            if years is not None:
                chip = _match_years_to_chip_option(int(years), options)
                if chip:
                    return chip
        picked = _normalize_to_option(text, options)
        if picked:
            return picked
        if years_field:
            num = re.search(r"(\d+)", text)
            if num:
                value = int(num.group(1))
                chip = _match_years_to_chip_option(value, options)
                if chip:
                    return chip
        if re.search(r"\b(yes|no)\b", text, re.I) and {o.lower() for o in options} <= {"yes", "no"}:
            return "Yes" if re.search(r"\byes\b", text, re.I) else "No"
        if {o.lower() for o in options} <= {"yes", "no"}:
            if re.search(r"\b(residing|relocate|living in|willing to)\b", label, re.I):
                if re.search(r"\b(no|not willing|cannot relocate)\b", text, re.I):
                    return "No"
                if re.search(r"\b(current|native)\b", text, re.I) or ":" in text:
                    return "Yes"
            if re.search(r"\bpan\b", label, re.I) and re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", text.strip(), re.I):
                return "Yes"

    if kind == "radio" and not options and re.search(r"\b(yes|no)\b", text, re.I):
        return "Yes" if re.search(r"\byes\b", text, re.I) else "No"

    input_type = infer_field_input_type(label, field)
    if input_type == "years_numeric" or is_skill_years_question(label):
        from .chips import coerce_yes_no_to_years_count

        num = parse_years_numeric_value(text)
        if num is not None:
            return str(int(num)) if num == int(num) else str(num)
        coerced = coerce_yes_no_to_years_count(text)
        if coerced is not None:
            return coerced

    # Text fields: append "years" when question asks for years and answer is numeric only
    if kind in ("input", "text", "textarea") and re.search(r"\byears?\b", label, re.I):
        if re.fullmatch(r"\d+", text):
            return f"{text} years"

    if re.search(r"\bnotice\s*period\b", label, re.I) or infer_field_input_type(label, field) == "notice_period":
        if re.fullmatch(r"0(?:\s*days?)?", text.strip(), re.I):
            if kind not in ("radio", "checkbox_group") or not options:
                return "Immediately available"

    return text


logger = logging.getLogger("job_apply")

_USER_CONFIRMED_SOURCES = frozenset({"pending", "manual", "confirmed", "interactive"})


def persist_answer(
    base_dir,
    question: str,
    fill_answer: str,
    field: dict[str, Any],
    config: AppConfig,
    *,
    company: str = "",
    job_title: str = "",
    canonical: str | None = None,
    reviewed: bool = True,
    source: str = "",
) -> tuple[str, str, bool]:
    """Save canonical answer to memory; return (canonical, fill_value, saved)."""
    from .validation import saved_answer_fits_field

    stored = canonical or canonicalize_stored_answer(question, fill_answer, field, config)
    fill = resolve_fill_answer(stored, field, config)
    if (source or "").strip() == "config":
        # Deterministic config/facts answers are re-derived from YAML each run;
        # keep them ephemeral so they never shadow a corrected YAML value.
        return stored, fill, False
    bad_meta = is_llm_meta_answer(stored)
    bad_quality = needs_review_answer(question, stored)
    user_confirmed = reviewed and source in _USER_CONFIRMED_SOURCES

    if bad_meta and not user_confirmed:
        logger.warning(
            "Refusing to persist meta answer for %r: %r",
            question[:50],
            stored[:80],
        )
        usable = answer_usable(question, stored, field, config)
        return stored, fill if usable else "", False

    if user_confirmed:
        save_answer(
            base_dir,
            question,
            stored,
            company=company,
            job_title=job_title,
            reviewed=True,
            needs_review=False,
            source=source,
            config=config,
        )
        return stored, fill, True

    if not saved_answer_fits_field(stored, field, config):
        logger.warning(
            "Refusing to persist answer that does not fit field for %r: %r",
            question[:50],
            stored[:80],
        )
        return stored, fill, False

    save_answer(
        base_dir,
        question,
        stored,
        company=company,
        job_title=job_title,
        reviewed=not bad_quality,
        needs_review=bad_quality,
        source=source,
        config=config,
    )
    if bad_quality:
        logger.info(
            "Persisted answer with needs_review for %r",
            question[:50],
        )
    return stored, fill, True


def flag_rejected_saved_answer(
    base_dir,
    question: str,
    rejected_answer: str,
) -> None:
    """Mark memory entries that supplied a rejected answer so they are not reused."""
    from ..question_groups import classify_question

    question = normalize_question_label(question)
    key = memory_key(question)
    group_id = classify_question(question)
    rejected = rejected_answer.strip()
    if not rejected:
        return

    def _apply(data: dict[str, Any]) -> None:
        answers = data.get("question_answers", {})
        for entry_key, entry in answers.items():
            if not isinstance(entry, dict):
                continue
            stored_q = str(entry.get("question", ""))
            ans = str(entry.get("answer", "")).strip()
            if ans != rejected:
                continue
            if entry_key == key or (stored_q and classify_question(stored_q) == group_id):
                entry["needs_review"] = True
                entry["reviewed"] = False

    mutate_memory(base_dir, _apply)


def _try_saved_entry(
    question: str,
    entry: dict[str, Any],
    field: dict[str, Any] | None,
    *,
    exact_match: bool,
    group_id: str = "",
    stored_q: str = "",
    config: AppConfig | None = None,
) -> str | None:
    if not _saved_entry_reusable(entry, exact_match=exact_match, group_id=group_id):
        return None
    if stored_q and group_id and not exact_match:
        if group_id == "compensation" and ctc_want_kind(stored_q) != ctc_want_kind(question):
            return None
        if not _skill_questions_compatible(stored_q, question, group_id):
            return None
    ans = str(entry.get("answer", "")).strip() or None
    if not ans:
        return None
    enriched = enrich_field_for_llm({**(field or {}), "label": question})
    if answer_usable(question, ans, enriched, config):
        return ans
    # City → Bangalore/Outside Bangalore (and similar) chip remaps: stored
    # canonical may not literally equal an option, but resolve_fill can map it.
    fill = resolve_fill_answer(ans, enriched, config)
    if fill and answer_usable(question, fill, enriched, config):
        return ans
    if (
        exact_match
        and entry.get("reviewed")
        and str(entry.get("source", ""))
        in (
            "manual",
            "pending",
            "confirmed",
            "interactive",
        )
    ):
        # The user explicitly reviewed/provided this answer for this exact
        # question. Trust it and never re-queue — coerce only to fit the field
        # format (e.g. range chips). Do not let "looks auto-generated / wrong"
        # heuristics discard a human-reviewed answer (e.g. "84.4" for a
        # percentage question), or the same question gets asked every run.
        from .validation import is_hard_type_mismatch, is_placeholder_answer

        if is_placeholder_answer(ans):
            return None
        if is_hard_type_mismatch(question, ans, enriched, config):
            # User may have answered "Yes"/"No" to a years field phrased as experience.
            if fill and answer_usable(question, fill, enriched, config):
                return fill
            return None
        return fill or ans
    return None


def build_answer_group_index(
    answers: dict[str, Any],
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Index saved answers by question group for fast lookup during apply."""
    from ..question_groups import classify_question

    index: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for entry in answers.values():
        if not isinstance(entry, dict):
            continue
        stored_q = str(entry.get("question", ""))
        if not stored_q:
            continue
        group_id = classify_question(stored_q)
        index.setdefault(group_id, []).append((stored_q, entry))
    return index


def _group_saved_entries(
    answers: dict[str, Any],
    group_index: dict[str, list[tuple[str, dict[str, Any]]]] | None,
    group_id: str,
) -> list[tuple[str, dict[str, Any]]]:
    if group_index is not None:
        return group_index.get(group_id, [])
    from ..question_groups import classify_question

    out: list[tuple[str, dict[str, Any]]] = []
    for entry in answers.values():
        if not isinstance(entry, dict):
            continue
        stored_q = str(entry.get("question", ""))
        if stored_q and classify_question(stored_q) == group_id:
            out.append((stored_q, entry))
    return out


def get_saved_answer(
    base_dir,
    question: str,
    field: dict[str, Any] | None = None,
    config: AppConfig | None = None,
    *,
    answers: dict[str, Any] | None = None,
    group_index: dict[str, list[tuple[str, dict[str, Any]]]] | None = None,
) -> str | None:
    from ..question_groups import classify_question

    question = normalize_question_label(question)
    if answers is None:
        answers = load_memory(base_dir).get("question_answers", {})
    # Canonical group key first, then legacy per-phrasing hash (back-compat).
    seen_keys: set[str] = set()
    for lookup_key in (memory_key(question), question_key(question)):
        if lookup_key in seen_keys:
            continue
        seen_keys.add(lookup_key)
        entry = answers.get(lookup_key)
        if isinstance(entry, dict):
            matched = _try_saved_entry(question, entry, field, exact_match=True, config=config)
            if matched:
                return matched

    group_id = classify_question(question)
    if group_id.startswith("unique:"):
        return None

    if group_id == "compensation":
        want = ctc_want_kind(question)
        best: tuple[float, str] | None = None
        for stored_q, entry in _group_saved_entries(answers, group_index, group_id):
            if ctc_want_kind(stored_q) != want:
                continue
            ans = _try_saved_entry(
                question,
                entry,
                field,
                exact_match=False,
                group_id=group_id,
                stored_q=stored_q,
                config=config,
            )
            if not ans:
                continue
            score = _score_stored_question_match(stored_q, question)
            if best is None or score > best[0]:
                best = (score, ans)
        if best:
            return best[1]
        return None

    if group_id in ("current_location", "preferred_location"):
        want_yesno = is_relocation_yesno_question(question)
        listed_cities: list[str] = []
        if group_id == "preferred_location" and not want_yesno:
            from ..rag_answers import _cities_from_question_label

            listed_cities = _cities_from_question_label(question)
        best = None
        for stored_q, entry in _group_saved_entries(answers, group_index, group_id):
            if is_relocation_yesno_question(stored_q) != want_yesno:
                continue
            ans = str(entry.get("answer", "")) or None
            if not ans:
                continue
            if listed_cities and not saved_location_answer_matches_question(question, ans):
                continue
            matched = _try_saved_entry(question, entry, field, exact_match=False, group_id=group_id, stored_q=stored_q)
            if not matched:
                continue
            score = _score_stored_question_match(stored_q, question)
            if best is None or score > best[0]:
                best = (score, matched)
        if best:
            return best[1]

    best = None
    for stored_q, entry in _group_saved_entries(answers, group_index, group_id):
        if not saved_location_answer_matches_question(question, str(entry.get("answer", ""))):
            continue
        matched = _try_saved_entry(question, entry, field, exact_match=False, group_id=group_id, stored_q=stored_q)
        if not matched:
            continue
        score = _score_stored_question_match(stored_q, question)
        if best is None or score > best[0]:
            best = (score, matched)
    return best[1] if best else None


# Source trust ranking — higher wins. Reconciliation never lets a weaker source
# silently overwrite a stronger, human-reviewed answer for the same group.
_SOURCE_RANK: dict[str, int] = {
    "manual": 5,
    "pending": 5,
    "confirmed": 5,
    "interactive": 5,
    "config": 4,
    "llm+verified": 3,
    "llm-verified": 3,
    "rag": 2,
    "vector": 2,
    "llm": 1,
    "llm-option": 1,
    "": 0,
}


def _source_rank(source: Any) -> int:
    return _SOURCE_RANK.get(str(source or "").strip().lower(), 1)


def memory_key(question: str) -> str:
    """Canonical storage key — collapse equivalent phrasings into one entry.

    Single-valued fact groups (skills, compensation, notice, …) share one entry
    so the same concept cannot drift into contradictory duplicates. Genuinely
    question-specific buckets (preferred-location city lists, truly unique
    questions) stay keyed per question.
    """
    from ..question_groups import classify_question

    q = normalize_question_label(question)
    gid = classify_question(q)
    if gid.startswith("unique:"):
        return gid
    if gid == "compensation":
        return f"compensation:{ctc_want_kind(q)}"
    if gid == "preferred_location" and not is_relocation_yesno_question(q):
        return f"preferred_location:q:{question_key(q)}"
    if gid in ("current_location", "preferred_location"):
        kind = "yesno" if is_relocation_yesno_question(q) else "value"
        return f"{gid}:{kind}"
    return gid


def save_answer(
    base_dir,
    question: str,
    answer: str,
    *,
    company: str = "",
    job_title: str = "",
    aliases: list[str] | None = None,
    reviewed: bool | None = None,
    needs_review: bool = False,
    source: str = "",
    config: AppConfig | None = None,
) -> None:
    from datetime import datetime, timezone

    primary = normalize_question_label(question)
    answer = answer.strip()
    examples_seen = [question.strip()]
    if aliases:
        for alias in aliases:
            alias = alias.strip()
            if alias and alias not in examples_seen:
                examples_seen.append(alias)
    new_rank = _source_rank(source)

    def _apply(data: dict[str, Any]) -> None:
        answers = data.setdefault("question_answers", {})
        key = memory_key(primary)
        existing = answers.get(key)
        entry: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}

        prev_ans = str(entry.get("answer", "")).strip()
        prev_reviewed = bool(entry.get("reviewed"))
        # Keep the stronger answer: don't let a weaker source overwrite a
        # human-reviewed one. Same-or-higher rank (or unreviewed prev) → update.
        keep_prev = bool(
            prev_ans and prev_ans != answer and prev_reviewed and _source_rank(entry.get("source")) > new_rank
        )

        examples = [str(e) for e in entry.get("examples", []) if str(e)]
        for ex in [primary, *examples_seen]:
            if ex and ex not in examples:
                examples.append(ex)
        entry["examples"] = examples[-10:]
        entry["hits"] = int(entry.get("hits", 0) or 0) + 1
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        entry.setdefault("question", primary)

        if not keep_prev:
            entry["question"] = primary
            entry["answer"] = answer
            entry["company"] = company
            entry["job_title"] = job_title
            if reviewed is True:
                entry["reviewed"] = True
                entry.pop("needs_review", None)
            elif reviewed is False:
                entry["reviewed"] = False
            if needs_review:
                entry["needs_review"] = True
                entry["reviewed"] = False
            if source:
                entry["source"] = source

        answers[key] = entry

    mutate_memory(base_dir, _apply, config)


def _saved_entry_reusable(
    entry: dict[str, Any],
    *,
    exact_match: bool,
    group_id: str = "",
) -> bool:
    """Group reuse requires reviewed=True; compensation is always reusable unless flagged."""
    if entry.get("needs_review"):
        return False
    if exact_match:
        return True
    if group_id == "compensation":
        return True
    stored_q = str(entry.get("question", ""))
    if stored_q:
        from ..question_groups import classify_question

        if classify_question(stored_q) == "compensation":
            return True
    return bool(entry.get("reviewed"))


def _skill_questions_compatible(stored_q: str, question: str, group_id: str) -> bool:
    if not (group_id.startswith("skill:") or group_id.startswith("skill_yesno:")):
        return True
    skill_part = group_id.split(":", 1)[1].replace("_", " ")
    stored_l = stored_q.lower()
    question_l = question.lower()
    skill_tokens = [t for t in re.findall(r"[a-z0-9+#./-]+", skill_part.lower()) if len(t) > 2]
    if skill_tokens and not all(any(tok in text for tok in skill_tokens) for text in (stored_l, question_l)):
        return False
    if skill_part and skill_part in stored_l and skill_part in question_l:
        return True
    stop = {
        "how",
        "many",
        "years",
        "experience",
        "have",
        "you",
        "your",
        "the",
        "with",
        "and",
        "for",
        "are",
        "any",
        "all",
        "our",
        "this",
        "that",
        "what",
        "which",
        "language",
        "domain",
        "domains",
        "knowledge",
        "capabilities",
    }
    st = {t for t in re.findall(r"[a-z0-9+#./-]+", stored_l) if len(t) > 2 and t not in stop}
    qt = {t for t in re.findall(r"[a-z0-9+#./-]+", question_l) if len(t) > 2 and t not in stop}
    overlap = st & qt
    if skill_tokens:
        return len(overlap) >= 1 and any(t in overlap for t in skill_tokens)
    return len(overlap) >= 2


def _score_stored_question_match(stored_q: str, question: str) -> float:
    stored_l = re.sub(r"\s+", " ", stored_q.strip().lower())
    question_l = re.sub(r"\s+", " ", question.strip().lower())
    if stored_l == question_l:
        return 3.0
    if stored_l in question_l or question_l in stored_l:
        return 2.0
    st = {t for t in re.findall(r"[a-z0-9+#./-]+", stored_l) if len(t) > 2}
    qt = {t for t in re.findall(r"[a-z0-9+#./-]+", question_l) if len(t) > 2}
    if not st or not qt:
        return 0.0
    return len(st & qt) / max(len(st), len(qt))
