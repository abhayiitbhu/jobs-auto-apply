"""When auto-generated drafts may be written to user_memory.json."""

from __future__ import annotations

from typing import Any

from ..config import AppConfig
from ..question_groups import classify_question
from .config_answers import authoritative_config_answer
from .experience import is_new_experience_question

# RAG answers backed only by application_facts / compensation (not resume keyword heuristics).
_DETERMINISTIC_FACT_GROUPS = frozenset(
    {
        "compensation",
        "notice_period",
        "pincode",
        "pan",
        "f2f_interview",
        "last_working_day",
        "prior_application",
        "join_availability",
        "uan",
        "current_location",
        "preferred_location",
    }
)


def _answers_match(expected: str, actual: str) -> bool:
    return expected.strip().lower() == actual.strip().lower()


def rag_rule_persist_confidence(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    answer: str,
) -> float:
    """1.0 when rule RAG mirrors config/facts; 0.0 for heuristic skill yes/no guesses."""
    auth = authoritative_config_answer(config, question, field)
    if auth and _answers_match(auth, answer):
        return 1.0
    group_id = classify_question(question)
    if group_id in _DETERMINISTIC_FACT_GROUPS:
        return 1.0
    if group_id.startswith("skill:"):
        return 1.0 if auth and _answers_match(auth, answer) else 0.0
    return 0.0


def effective_persist_threshold(config: AppConfig, question: str) -> float:
    threshold = config.llm.min_confidence_persist
    if is_new_experience_question(config, question):
        threshold = max(threshold, config.llm.min_confidence_new_experience)
    return threshold


def draft_should_persist_to_memory(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    source: str,
    confidence: float,
    answer: str,
) -> bool:
    """Only persist auto-drafts when confidence is very high or answer is facts-backed."""
    if not config.llm.auto_save:
        return False

    src = (source or "").strip()
    if src == "config":
        # Config / application_facts answers are deterministic and re-derived from
        # YAML every run. Persisting them creates a stale shadow that can later
        # beat the corrected YAML value, so we keep them ephemeral.
        return False

    if src == "LLM-option":
        # Constrained option picks are valid to fill for the current job, but the
        # right choice can depend on that job's specific option set, so we do NOT
        # persist them to memory — they are re-selected per application.
        return False

    threshold = effective_persist_threshold(config, question)

    # All accepted free-text LLM answers are now corroborated (LLM+RAG / LLM+vector
    # / LLM+verified); persist on the corroboration-derived confidence.
    if src.startswith("LLM"):
        return confidence >= threshold

    if src == "vector":
        return confidence >= threshold

    if src == "RAG":
        return rag_rule_persist_confidence(config, question=question, field=field, answer=answer) >= threshold

    return False
