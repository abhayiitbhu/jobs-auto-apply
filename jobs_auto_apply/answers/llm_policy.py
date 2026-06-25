"""Tiered LLM confidence policy (config-driven thresholds)."""

from __future__ import annotations

import logging
from typing import Any

from ..config import AppConfig
from ..question_groups import classify_question
from .experience import is_new_experience_question
from .validation import answer_acceptable_for_field, answers_equivalent_for_agreement

logger = logging.getLogger("job_apply")

# Vector "consensus": when several already-fetched similar past answers agree on the
# same value, that is corroboration for free (no extra model call). Members must be
# same-group and reasonably similar so a generic "Yes" doesn't create false consensus.
_VECTOR_CONSENSUS_MIN_SCORE = 0.70
_VECTOR_CONSENSUS_MIN_COUNT = 2


def effective_min_confidence(
    config: AppConfig,
    question: str,
    field: dict[str, Any],
) -> float:
    if is_new_experience_question(config, question):
        return config.llm.min_confidence_new_experience
    return config.llm.min_confidence


def input_type_allows_rag_agree(config: AppConfig, input_type: str) -> bool:
    return input_type in config.llm.rag_agree_input_types


def derive_effective_confidence(
    config: AppConfig,
    decision: Any,
    *,
    rag_agreed: bool = False,
    vector_agreed: bool = False,
    vector_score: float = 0.0,
    verified: bool = False,
) -> float:
    """Replace the model's self-reported number with a corroboration-derived one.

    A small local model's self-reported confidence is unreliable, so we only treat
    an answer as high-confidence when an *independent* source backs it:
      - RAG rule / facts agreement → strongest
      - a similar past answer (vector) agreement, scaled by similarity
      - the verifier model explicitly approved the answer
    With no corroboration we keep the answer for the current fill but cap its
    effective confidence below the persist threshold, so it isn't saved on the
    model's word alone.
    """
    self_reported = float(getattr(decision, "confidence", 0.0) or 0.0)

    signals: list[float] = []
    if rag_agreed:
        signals.append(0.97)
    if vector_agreed:
        # A match exactly at the floor is weaker than a near-identical one.
        floor = config.llm.vector_agree_score
        span = max(1e-6, 1.0 - floor)
        scaled = 0.88 + 0.10 * max(0.0, min(1.0, (vector_score - floor) / span))
        signals.append(scaled)
    if verified:
        signals.append(0.95)

    if signals:
        # Corroborated: trust the strongest independent signal (never below what the
        # model itself claimed).
        return round(min(1.0, max(self_reported, max(signals))), 4)

    # Uncorroborated: cap so it can fill but won't auto-persist.
    return round(min(self_reported, config.llm.uncorroborated_confidence_cap), 4)


def _vector_consensus_agrees(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    similar_answers: list[Any] | None,
    answer: str,
) -> bool:
    """True when >=2 already-fetched, same-group similar answers match ``answer``.

    Uses only data already retrieved (no extra model call), so it is a free
    corroboration source: agreement among several past answers is strong evidence.
    """
    if not similar_answers:
        return False
    current_group = classify_question(question)
    agree = 0
    for item in similar_answers:
        score = float(getattr(item, "score", 0.0) or 0.0)
        past_q = str(getattr(item, "question", "") or "")
        past_a = str(getattr(item, "answer", "") or "")
        if not past_a or score < _VECTOR_CONSENSUS_MIN_SCORE:
            continue
        if classify_question(past_q) != current_group:
            continue
        if answers_equivalent_for_agreement(question, field, past_a, answer, config):
            agree += 1
            if agree >= _VECTOR_CONSENSUS_MIN_COUNT:
                return True
    return False


def llm_decision_acceptable(
    config: AppConfig,
    decision: Any,
    *,
    question: str,
    field: dict[str, Any],
    rag_hint: str | None = None,
    vector_hint: str | None = None,
    vector_score: float = 0.0,
    similar_answers: list[Any] | None = None,
) -> tuple[bool, str, float]:
    if not answer_acceptable_for_field(question, decision.answer, field):
        return False, "", 0.0

    new_exp = is_new_experience_question(config, question)

    rag_agreed = bool(
        rag_hint
        and answers_equivalent_for_agreement(
            question, field, rag_hint, decision.answer, config
        )
    )
    vector_agreed = bool(
        vector_hint
        and vector_score >= config.llm.vector_agree_score
        and answers_equivalent_for_agreement(
            question, field, vector_hint, decision.answer, config
        )
    ) or _vector_consensus_agrees(
        config,
        question=question,
        field=field,
        similar_answers=similar_answers,
        answer=decision.answer,
    )

    # An LLM answer is only used when an INDEPENDENT second source backs it. We check
    # the free signals first (rule/RAG agreement, vector match/consensus) and only
    # invoke the verifier model when none of them corroborate — so verification is
    # lazy and we avoid a redundant model call when agreement already exists.
    verified = False
    if not (rag_agreed or vector_agreed):
        verified = _verifier_backs_answer(
            config,
            question=question,
            field=field,
            answer=decision.answer,
            similar_answers=similar_answers,
            rag_hint=rag_hint,
        )

    if not (rag_agreed or vector_agreed or verified):
        logger.info(
            "LLM answer lacks a second source (RAG/vector/verifier) — queuing: %s",
            question[:60],
        )
        return False, "", 0.0

    if rag_agreed:
        source = "LLM+RAG"
    elif vector_agreed:
        source = "LLM+vector"
    else:
        source = "LLM+verified"

    conf = derive_effective_confidence(
        config,
        decision,
        rag_agreed=rag_agreed,
        vector_agreed=vector_agreed,
        vector_score=vector_score,
        verified=verified,
    )
    logger.info(
        "LLM accepted via %s%s (effective confidence %.2f): %s",
        source,
        " [new-experience]" if new_exp else "",
        conf,
        question[:60],
    )
    return True, source, conf


def _verifier_backs_answer(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    answer: str,
    similar_answers: list[Any] | None,
    rag_hint: str | None,
) -> bool:
    """Lazy verifier call (only when no free corroboration). True if approved."""
    from ..llm_answers import _build_application_context, verify_llm_answer_detailed

    _, verified = verify_llm_answer_detailed(
        config,
        question=question,
        field=field,
        fill_answer=answer,
        profile_excerpt=_build_application_context(config),
        similar_answers=similar_answers,
        rag_hint=rag_hint,
    )
    return verified
