"""Canonical vs fill formatting — always run before persisting or filling a form."""

from __future__ import annotations

from typing import Any

from ..answer_suggest import is_employer_check_question, is_prior_application_screening
from ..config import AppConfig
from .compensation import looks_like_compensation_question
from .experience import is_new_experience_question, is_skill_years_question
from .fields import enrich_field_for_llm, infer_field_input_type, is_numeric_ctc_question
from .memory_store import canonicalize_stored_answer, resolve_fill_answer
from .validation import answer_acceptable_for_field


def is_high_risk_question(
    config: AppConfig,
    question: str,
    field: dict[str, Any],
) -> bool:
    """Fields where a wrong auto-answer is costly — run optional verifier model."""
    label = str(field.get("label", question) or question)
    if is_employer_check_question(label) or is_prior_application_screening(label):
        return True
    if looks_like_compensation_question(label) or is_numeric_ctc_question(label):
        return True
    if is_skill_years_question(label) or is_new_experience_question(config, label):
        return True
    input_type = infer_field_input_type(label, field)
    if input_type in ("ctc_numeric", "years_numeric"):
        return True
    return False


def finalize_answer_for_field(
    question: str,
    field: dict[str, Any],
    config: AppConfig | None,
    *,
    raw_answer: str,
    canonical: str | None = None,
) -> tuple[str, str] | None:
    """
    Map raw/semantic answer → canonical (store) + fill (form).
    Returns None when the value cannot be shaped for this field.
    """
    text = (raw_answer or "").strip()
    if not text:
        return None

    field = enrich_field_for_llm(field)
    stored = (canonical or "").strip() or canonicalize_stored_answer(
        question, text, field, config
    )
    if not stored:
        return None

    fill = resolve_fill_answer(stored, field, config)
    if not fill or not answer_acceptable_for_field(question, fill, field):
        return None
    return fill, stored
