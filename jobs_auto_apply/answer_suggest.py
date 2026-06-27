from __future__ import annotations

import re
from typing import Any

from .config import AppConfig

_EMPLOYER_CHECK = re.compile(
    r"\b(associated with|previously employed|currently employed at|employee of|"
    r"employed by|worked (?:at|for|with)|previously worked|employed with us|"
    r"worked for us|applied previously|received an offer from|military spouse|"
    r"identify as a military)\b",
    re.I,
)

_PRIOR_APPLICATION_SCREENING = re.compile(
    r"\b(profile previously uploaded|interview attended|previously applied|"
    r"applied before|can not process|cannot process)\b",
    re.I,
)


def is_employer_check_question(question: str) -> bool:
    return bool(_EMPLOYER_CHECK.search(question))


def is_prior_application_screening(question: str) -> bool:
    return bool(_PRIOR_APPLICATION_SCREENING.search(question))


def field_for_question(question: str, field: dict[str, Any] | None = None) -> dict[str, Any]:
    if field:
        return field
    if is_employer_check_question(question) or is_prior_application_screening(question):
        return {"kind": "radio", "label": question, "options": ["Yes", "No"]}
    return {"kind": "text", "label": question}


def suggest_answer(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any] | None = None,
    company: str = "",
) -> str | None:
    """Draft via rule RAG → vector → Ollama; returns fill value for the form."""
    field = field_for_question(question, field)
    from .answers.draft import draft_answer_for_field

    result = draft_answer_for_field(
        config,
        question=question,
        field=field,
        company=company,
    )
    return result.fill
