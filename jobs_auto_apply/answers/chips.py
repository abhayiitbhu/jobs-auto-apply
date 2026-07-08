"""Chip / range option parsing and years extraction."""

from __future__ import annotations

import re

from .chip_options import (
    value_in_chip_range,
)

_YEAR_CHIP_LABEL = re.compile(r"<\s*\d+|\d+\s*[-–]\s*\d+|\d+\s*\+", re.I)

_YEAR_EXPERIENCE_Q = re.compile(
    r"\b(how many|years?)\b.*\b(experience|hands?\s*on)\b|\bexperience\b.*\byears?\b",
    re.I,
)


def _value_in_answer_range(value: int, option: str) -> bool:
    return value_in_chip_range(value, option)


def is_chip_range_label(answer: str) -> bool:
    """True when answer is a UI chip label (e.g. '<6 years') rather than a canonical value."""
    return bool(_YEAR_CHIP_LABEL.search(answer.strip()))


def coerce_yes_no_to_years_count(text: str) -> str | None:
    """Map a bare yes/no reply to a years count for numeric experience fields."""
    low = text.strip().lower()
    if re.match(r"^(yes|y|yeah|yep|yup|true)\b", low):
        return "1"
    if re.match(r"^(no|n|none|nope|false)\b", low):
        return "0"
    return None


def parse_years_numeric_value(answer: str) -> float | None:
    """Extract a years count from answers like '5', '5 years', or '0 years of X'."""
    text = answer.strip()
    if not text:
        return None
    plain = re.fullmatch(r"(\d+(?:\.\d+)?)(?:\s*years?)?", text, re.I)
    if plain:
        return float(plain.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)\s*years?", text, re.I)
    if match:
        return float(match.group(1))
    lone = re.fullmatch(r"(\d+(?:\.\d+)?)", text)
    if lone:
        return float(lone.group(1))
    return None


def _no_experience_option(options: list[str]) -> str | None:
    for opt in options:
        if re.search(r"\bno experience\b", opt, re.I):
            return opt.strip()
    return None


_IMMEDIATE_NOTICE = re.compile(
    r"\bimmediate\b|\bimmediately\b|join\s*immediately|(?:^|\b)0\s*days?\b|" r"\bavailable\s*now\b|\bright\s*away\b",
    re.I,
)
_SHORT_NOTICE = re.compile(r"15\s*days?\s*or\s*less", re.I)


def pick_notice_period_option(answer: str, options: list[str]) -> str | None:
    """Map notice-period answers to Hirist/Naukri radio options."""
    a = answer.lower().strip()
    if re.fullmatch(r"0(?:\s*days?)?", a) or re.search(r"\b(immediate|immediately|available now)\b", a):
        for opt in options:
            if _IMMEDIATE_NOTICE.search(opt):
                return opt.strip()
        for opt in options:
            if re.search(r"\b1\s*week\b", opt, re.I):
                return opt.strip()
        for opt in options:
            if _SHORT_NOTICE.search(opt):
                return opt.strip()
        for opt in options:
            if re.search(r"\b15\s*days?\b", opt, re.I):
                return opt.strip()
    month_m = re.search(r"(\d+)\s*month", a)
    if month_m:
        months = month_m.group(1)
        for opt in options:
            if re.search(rf"\b{months}\s*month", opt, re.I):
                return opt.strip()
    day_m = re.search(r"(\d+)\s*days?", a)
    if day_m:
        days = int(day_m.group(1))
        if days == 0:
            for opt in options:
                if _IMMEDIATE_NOTICE.search(opt):
                    return opt.strip()
        if days <= 7:
            for opt in options:
                if re.search(r"\b1\s*week\b", opt, re.I):
                    return opt.strip()
        if days <= 15:
            for opt in options:
                if _SHORT_NOTICE.search(opt):
                    return opt.strip()
            for opt in options:
                if re.search(r"\b15\s*days?\b", opt, re.I):
                    return opt.strip()
        if days <= 30:
            for opt in options:
                if re.search(r"\b1\s*month", opt, re.I):
                    return opt.strip()
            for opt in options:
                if re.search(r"\b30\s*days?\b", opt, re.I):
                    return opt.strip()
        if days <= 60:
            for opt in options:
                if re.search(r"\b2\s*month", opt, re.I):
                    return opt.strip()
        if days <= 90:
            for opt in options:
                if re.search(r"\b3\s*month", opt, re.I):
                    return opt.strip()
        for opt in options:
            if re.search(r"\babove\s*30\b", opt, re.I):
                return opt.strip()
    week_m = re.search(r"(\d+)\s*week", a)
    if week_m:
        weeks = int(week_m.group(1))
        for opt in options:
            if re.search(rf"\b{weeks}\s*week", opt, re.I):
                return opt.strip()
    return None


def _match_years_to_chip_option(value: int, options: list[str]) -> str | None:
    """Map a years count to a Naukri-style range chip (e.g. 4 → '4-6 years')."""
    if value == 0:
        return _no_experience_option(options)
    for opt in options:
        if _value_in_answer_range(value, opt):
            return opt
    return None


def _normalize_to_option(answer: str, options: list[str]) -> str | None:
    a = answer.strip().lower()
    if not a:
        return None
    if a in ("0", "0 years", "no experience"):
        return _no_experience_option(options)
    for opt in options:
        o = opt.strip()
        ol = o.lower()
        if ol == a:
            return o
        if re.fullmatch(r"\d+(?:\.\d+)?", a):
            continue
        if a in ol or ol in a:
            return o
    return None
