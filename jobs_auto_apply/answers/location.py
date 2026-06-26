"""Location question detection and answer fitting."""

from __future__ import annotations

import re
from typing import Any

from ..answer_suggest import is_employer_check_question
from ..question_groups import classify_question
from ..rag_answers import _cities_from_question_label, _city_name_matches
from .chips import is_chip_range_label


def _is_relocation_yesno_question(label: str) -> bool:
    return bool(
        re.search(
            r"\b(willing to relocate|currently living|currently residing|"
            r"open to relocate|ready to relocate|living in)\b",
            label,
            re.I,
        )
    )


def _is_location_value_question(label: str) -> bool:
    gid = classify_question(label)
    if gid == "current_location":
        return True
    if gid == "preferred_location":
        return not _is_relocation_yesno_question(label)
    return False


def _location_like_answer_fits(label: str, answer: str, field: dict[str, Any] | None = None) -> bool:
    """Reject years counts, Yes/No, and employer prose on city/location fields."""
    text = answer.strip()
    if not text:
        return False
    lower = text.lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", text) or is_chip_range_label(text):
        return False
    if is_employer_check_question(text) or re.search(
        r"\bemployment with\b|\bprevious employment\b|\bpreviously (worked|employed)\b",
        lower,
    ):
        return False
    if _is_relocation_yesno_question(label):
        return bool(re.match(r"^(yes|no)\b", lower)) or lower in ("yes", "no")
    if re.match(r"^(yes|no)\b", lower):
        if field:
            options = [str(o).strip().lower() for o in field.get("options", []) if str(o).strip()]
            if lower in options:
                return True
        return False
    return len(text) >= 2


def _location_answer_fits(answer: str) -> bool:
    return _location_like_answer_fits("", answer)


def _saved_location_answer_matches_question(question: str, answer: str) -> bool:
    if classify_question(question) != "preferred_location":
        return True
    if _is_relocation_yesno_question(question):
        return True
    listed_cities = _cities_from_question_label(question)
    if not listed_cities:
        return True
    compact = re.sub(r"\s+", " ", answer).strip()
    if "," in compact or len(compact) > 30:
        return False
    return any(_city_name_matches(city, compact) for city in listed_cities)


# Public aliases
is_relocation_yesno_question = _is_relocation_yesno_question
is_location_value_question = _is_location_value_question
location_like_answer_fits = _location_like_answer_fits
saved_location_answer_matches_question = _saved_location_answer_matches_question
