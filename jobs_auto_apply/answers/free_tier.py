"""Collect rule/config/vector context once per question (no duplicate work before LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..llm_answers import _MIN_RAG_FOR_LLM, SimilarAnswer, retrieve_similar_answers
from ..question_groups import classify_question
from .config_answers import authoritative_config_answer
from .fields import enrich_field_for_llm
from .format_finalize import finalize_answer_for_field


@dataclass
class FreeTierContext:
    """Outputs from cheap tiers — fed to LLM when auto-fill did not succeed."""

    rag_auto: tuple[str, str] | None = None  # (fill, stored) when rule RAG passes format check
    config_hint: str | None = None
    similar_answers: list[SimilarAnswer] | None = None
    vector_best: SimilarAnswer | None = None

    @property
    def rag_fill(self) -> str | None:
        return self.rag_auto[0] if self.rag_auto else None


def _best_similar_match(
    config: AppConfig,
    question: str,
    candidates: list[SimilarAnswer],
    *,
    require_same_group: bool,
) -> SimilarAnswer | None:
    if not candidates:
        return None
    current_group = classify_question(question)
    best: SimilarAnswer | None = None
    for item in candidates:
        if require_same_group and classify_question(item.question) != current_group:
            continue
        if best is None or item.score > best.score:
            best = item
    return best


def collect_free_tier_context(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
) -> FreeTierContext:
    """Run config + rule RAG + vector search once; reuse for auto-fill and LLM hints."""
    field = enrich_field_for_llm(field)
    ctx = FreeTierContext()

    ctx.config_hint = authoritative_config_answer(config, question, field)

    if config.application.rag_answer_questions:
        from ..rag_answers import generate_rag_answer

        rag = generate_rag_answer(
            config,
            question=question,
            field=field,
        )
        if rag:
            # Only a rule-RAG answer that passes the field format check (rag_auto)
            # is used — as a direct fill or an LLM hint/corroboration source. The
            # raw value is intentionally dropped when finalize fails so an
            # unvalidated answer can't bias or circularly confirm the LLM.
            finalized = finalize_answer_for_field(question, field, config, raw_answer=rag)
            if finalized:
                ctx.rag_auto = finalized

    if config.llm.use_faiss_memory:
        k = max(config.llm.rag_top_k, _MIN_RAG_FOR_LLM)
        similar = retrieve_similar_answers(config, question, k=k)
        # For `unique:` questions there is no shared semantic group, so a lexically
        # similar past answer (e.g. another question that merely mentions the same
        # company/skill) is not a real precedent. Surfacing it as an LLM hint causes
        # the model to copy a topically-related but wrong answer. Restrict hints to
        # the exact same group so unique questions only see truly matching precedents.
        if classify_question(question).startswith("unique:"):
            current_group = classify_question(question)
            similar = [item for item in similar if classify_question(item.question) == current_group]
        ctx.similar_answers = similar
        ctx.vector_best = _best_similar_match(config, question, ctx.similar_answers, require_same_group=True)

    return ctx
