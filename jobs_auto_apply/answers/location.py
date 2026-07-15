"""Location question detection and answer fitting."""

from __future__ import annotations

import re
from typing import Any

from ..answer_suggest import is_employer_check_question
from ..question_groups import classify_question
from .chips import is_chip_range_label

_BANGALORE_ALIASES = frozenset({"bangalore", "bengaluru", "blr"})


def cities_from_question_label(question: str) -> list[str]:
    """Parse city names from labels like 'Preferred Location (Bengaluru/Trivandrum)'."""
    match = re.search(r"\(([^)]+)\)", question)
    if not match:
        return []
    return [part.strip() for part in re.split(r"[/,|]", match.group(1)) if part.strip()]


def city_name_matches(a: str, b: str) -> bool:
    x, y = a.strip().lower(), b.strip().lower()
    if not x or not y:
        return False
    if x == y or x in y or y in x:
        return True
    aliases = {
        "bengaluru": ("bangalore", "bengaluru"),
        "bangalore": ("bangalore", "bengaluru"),
        "gurugram": ("gurgaon", "gurugram"),
        "gurgaon": ("gurgaon", "gurugram"),
    }
    for left, rights in aliases.items():
        if x == left and any(r in y for r in rights):
            return True
        if y == left and any(r in x for r in rights):
            return True
    return False


# Back-compat aliases used by rag_answers / callers
_cities_from_question_label = cities_from_question_label
_city_name_matches = city_name_matches


def _is_relocation_yesno_question(label: str) -> bool:
    return bool(
        re.search(
            r"\b(willing to relocate|currently living|currently residing|"
            r"open to relocate|ready to relocate|living in|"
            r"mandatory to relocate|work[\s-]?from[\s-]?office|\bwfo\b|"
            r"comfortable (?:with|to)|(?:okay|ok) with|"
            r"open for.{0,40}location|relocate to|job location|"
            r"work from.{0,40}location|willing to work from|"
            r"interested in working.{0,40}(?:office|location|wfo))\b",
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


def _city_token(answer: str) -> str:
    """Strip Current:/Native: prefixes and take the primary city token."""
    text = re.sub(r"\s+", " ", answer.strip())
    text = re.sub(r"^(current|native)\s*:\s*", "", text, flags=re.I)
    if ";" in text:
        text = text.split(";", 1)[0]
    return text.strip()


def is_in_out_city_location_options(options: list[str]) -> bool:
    """True for chips like Bangalore / Outside Bangalore."""
    opts = [str(o).strip() for o in options if str(o).strip()]
    if len(opts) < 2:
        return False
    opts_l = [o.lower() for o in opts]
    has_city = any(re.search(r"\b(bangalore|bengaluru)\b", o) and not re.search(r"\boutside\b", o) for o in opts_l)
    has_outside = any(re.search(r"\boutside\b.{0,20}\b(bangalore|bengaluru)\b", o) for o in opts_l)
    if not has_outside:
        has_outside = any(re.search(r"\boutside\b", o) for o in opts_l) and has_city
    return bool(has_city and has_outside)


def map_city_to_location_chip(city_answer: str, options: list[str]) -> str | None:
    """Map a stored city (e.g. Bengaluru) onto Bangalore / Outside Bangalore chips."""
    opts = [str(o).strip() for o in options if str(o).strip()]
    if not city_answer or not opts:
        return None
    if not is_in_out_city_location_options(opts):
        return None

    token = _city_token(city_answer).lower()
    if not token or re.fullmatch(r"yes|no", token):
        return None

    in_bangalore = any(alias == token or alias in token or token in alias for alias in _BANGALORE_ALIASES) or any(
        city_name_matches(alias, token) for alias in ("bangalore", "bengaluru")
    )

    for opt in opts:
        ol = opt.lower()
        if in_bangalore:
            if re.search(r"\b(bangalore|bengaluru)\b", ol) and not re.search(r"\boutside\b", ol):
                return opt
        elif re.search(r"\boutside\b", ol):
            return opt
    return None


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
    if field:
        options = [str(o).strip() for o in field.get("options", []) if str(o).strip()]
        if options and map_city_to_location_chip(text, options):
            return True
    if re.match(r"^(yes|no)\b", lower):
        if field:
            options = [str(o).strip().lower() for o in field.get("options", []) if str(o).strip()]
            if lower in options:
                return True
        return False
    return len(text) >= 2


def _saved_location_answer_matches_question(question: str, answer: str) -> bool:
    if classify_question(question) != "preferred_location":
        return True
    if _is_relocation_yesno_question(question):
        return True
    listed_cities = cities_from_question_label(question)
    if not listed_cities:
        return True
    compact = re.sub(r"\s+", " ", answer).strip()
    if "," in compact or len(compact) > 30:
        return False
    return any(city_name_matches(city, compact) for city in listed_cities)


# Public aliases
is_relocation_yesno_question = _is_relocation_yesno_question
is_location_value_question = _is_location_value_question
location_like_answer_fits = _location_like_answer_fits
saved_location_answer_matches_question = _saved_location_answer_matches_question
