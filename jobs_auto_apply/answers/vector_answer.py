"""Vector top-1 auto-answer from FAISS memory (canonical → fill for this field)."""

from __future__ import annotations

import logging
from typing import Any

from ..config import AppConfig
from ..question_groups import classify_question
from .fields import enrich_field_for_llm
from .format_finalize import finalize_answer_for_field

logger = logging.getLogger("job_apply")


def try_vector_auto_answer(
    config: AppConfig,
    question: str,
    field: dict[str, Any],
    *,
    vector_best: Any | None = None,
) -> tuple[str, str, str, float] | None:
    """
    Reuse a highly similar past answer when vector score + question group match.
    Returns (fill_answer, canonical, source, score) or None.
    """
    if not config.llm.use_faiss_memory:
        return None

    from ..llm_answers import retrieve_best_similar_answer, verify_llm_answer

    field = enrich_field_for_llm(field)
    match = vector_best
    if match is None:
        match = retrieve_best_similar_answer(config, question, require_same_group=True)
    if not match:
        return None

    threshold = config.llm.vector_auto_answer_score
    if match.score < threshold:
        logger.debug(
            "Vector match below threshold (%.3f < %.3f): %s",
            match.score,
            threshold,
            question[:60],
        )
        return None

    finalized = finalize_answer_for_field(
        question,
        field,
        config,
        raw_answer=match.answer,
    )
    if not finalized:
        logger.info(
            "Vector top-1 rejected after format check (score=%.3f): %s",
            match.score,
            question[:60],
        )
        return None

    fill, stored = finalized
    from ..llm_answers import _build_application_context

    profile = _build_application_context(config)
    if not verify_llm_answer(
        config,
        question=question,
        field=field,
        fill_answer=fill,
        profile_excerpt=profile,
    ):
        return None

    logger.info(
        "Vector top-1 auto-answer (score=%.3f, group=%s): %s",
        match.score,
        classify_question(question),
        question[:60],
    )
    return fill, stored, "vector", match.score
