from __future__ import annotations

import re
from typing import Any

from .config import AppConfig

_EMPLOYER_CHECK = re.compile(
    r"\b(associated with|previously employed|currently employed at|employee of|"
    r"employed by|worked (?:at|for)|received an offer from|military spouse|"
    r"identify as a military)\b",
    re.I,
)


def is_employer_check_question(question: str) -> bool:
    return bool(_EMPLOYER_CHECK.search(question))


def field_for_question(question: str, field: dict[str, Any] | None = None) -> dict[str, Any]:
    if field:
        return field
    if is_employer_check_question(question):
        return {"kind": "radio", "label": question, "options": ["Yes", "No"]}
    return {"kind": "text", "label": question}


def suggest_answer(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any] | None = None,
    job_title: str = "",
    company: str = "",
    jd: str = "",
) -> str | None:
    """RAG rules first, then LLM with resume context (confidence-gated)."""
    field = field_for_question(question, field)

    from .rag_answers import generate_rag_answer

    rag = generate_rag_answer(
        config,
        question=question,
        field=field,
        jd=jd,
        job_title=job_title,
    )
    if rag:
        return rag

    if not config.llm.enabled:
        return None

    from .llm_answers import generate_llm_decision

    decision = generate_llm_decision(
        config,
        question=question,
        field=field,
        jd=jd,
        job_title=job_title,
        company=company,
    )
    if decision and decision.confidence >= config.llm.min_confidence:
        return decision.answer
    return None
