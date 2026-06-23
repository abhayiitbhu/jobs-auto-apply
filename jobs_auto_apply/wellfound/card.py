from __future__ import annotations

import re

from ..salary import extract_salary_from_text

_ROLE_LINE = re.compile(
    r"\b(engineer|developer|architect|lead|manager|analyst|intern|sde|devops|"
    r"programmer|consultant|designer)\b",
    re.I,
)


def pick_job_title_from_card(lines: list[str]) -> str:
    """Prefer the line that looks like a role title, not the company name."""
    for line in lines:
        if _ROLE_LINE.search(line):
            return line
    return lines[0] if lines else ""


def meta_from_card_text(text: str) -> dict:
    """Capture salary from the search-feed card before opening the job page."""
    salary = extract_salary_from_text(text)
    if not salary:
        return {}
    return {"salary_display": salary, "card_text": text[:800]}
