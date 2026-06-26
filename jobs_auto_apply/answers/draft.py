from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..question_keys import question_key
from .fields import enrich_field_for_llm
from .free_tier import FreeTierContext, collect_free_tier_context
from .llm_policy import llm_decision_acceptable
from .persist_policy import rag_rule_persist_confidence
from .vector_answer import try_vector_auto_answer

logger = logging.getLogger("job_apply")

_draft_cache_lock = threading.Lock()
_draft_cache: dict[str, DraftResult] = {}


@dataclass(frozen=True)
class DraftResult:
    fill: str | None = None
    canonical: str | None = None
    source: str = ""
    confidence: float = 0.0

    @property
    def has_answer(self) -> bool:
        return bool(self.fill)


def clear_draft_answer_cache() -> None:
    """Clear in-run LLM draft cache (call at start of each apply run)."""
    from ..llm_answers import clear_profile_context_cache

    with _draft_cache_lock:
        _draft_cache.clear()
    clear_profile_context_cache()


def _draft_cache_key(question: str, field: dict[str, Any]) -> str:
    enriched = enrich_field_for_llm(field)
    return "|".join(
        (
            question_key(question),
            str(enriched.get("kind", "")),
            str(enriched.get("input_type", "")),
            str(enriched.get("platform", "")),
        )
    )


def _cache_result(cache_key: str, result: DraftResult) -> DraftResult:
    if result.has_answer:
        with _draft_cache_lock:
            _draft_cache[cache_key] = result
    return result


def _try_rule_rag_from_context(
    config: AppConfig,
    ctx: FreeTierContext,
    *,
    question: str,
    field: dict[str, Any],
) -> DraftResult | None:
    if not ctx.rag_auto:
        return None
    fill, stored = ctx.rag_auto
    confidence = rag_rule_persist_confidence(config, question=question, field=field, answer=stored)
    logger.info("Rule RAG answer: %s", question[:60])
    return DraftResult(fill=fill, canonical=stored, source="RAG", confidence=confidence)


def _rag_hint_from_context(ctx: FreeTierContext) -> str | None:
    return ctx.rag_fill or ctx.rag_raw


_CHOICE_KINDS = {
    "radio",
    "checkbox",
    "checkbox_group",
    "single_choice",
    "multi_choice",
    "select",
    "dropdown",
}
# Options on these field types are hints/units, not a set to choose from.
_NON_CHOICE_INPUT_TYPES = {
    "ctc_numeric",
    "years_numeric",
    "number",
    "pincode",
    "date",
    "short_text",
    "long_text",
    "email",
    "url",
    "contenteditable",
}


def _field_options(field: dict[str, Any]) -> list[str]:
    opts = field.get("options") or field.get("answer_options") or []
    return [str(o).strip() for o in opts if str(o).strip()]


def _is_choice_field(field: dict[str, Any]) -> bool:
    """True for options-based questions where the answer must be one of N options."""
    opts = _field_options(field)
    if len(opts) < 2:
        return False
    input_type = str(field.get("input_type", "")).lower()
    if input_type in _NON_CHOICE_INPUT_TYPES:
        return False
    kind = str(field.get("kind", "")).lower()
    if kind in _CHOICE_KINDS:
        return True
    return input_type in {"single_choice", "multi_choice", "yes_no_checkbox"}


def _selection_context(free_tier: FreeTierContext) -> str | None:
    """Compact RAG/rule + prior-answer hints to inform option selection."""
    parts: list[str] = []
    hint = free_tier.rag_fill or free_tier.rag_raw or free_tier.config_hint
    if hint:
        parts.append(f"Suggested answer from rules/RAG: {hint}")
    for sa in (free_tier.similar_answers or [])[:3]:
        ans = getattr(sa, "answer", "")
        ques = getattr(sa, "question", "")
        if ans:
            parts.append(f"Previously answered {ques[:50]!r} -> {ans[:50]!r}")
    return "\n".join(parts) if parts else None


def _llm_select_choice(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    free_tier: FreeTierContext,
) -> DraftResult:
    """Answer determination for options-based questions: pick the best option(s)
    directly from the list instead of generating free text and converting it."""
    from ..llm_answers import select_options_for_question

    opts = _field_options(field)
    is_multi = str(field.get("kind", "")).lower() in ("checkbox_group", "multi_choice")
    chosen = select_options_for_question(
        config,
        question=question,
        options=opts,
        multi=is_multi,
        extra_context=_selection_context(free_tier),
    )
    if not chosen:
        return DraftResult()
    value = ", ".join(chosen) if is_multi else chosen[0]

    # An option pick must also be backed by an independent second source — a
    # rule/RAG/prior-answer hint that resolves to the same option, or the verifier
    # approving it against profile + databank. Otherwise queue for manual input.
    if not _option_pick_has_backing(config, question=question, field=field, value=value, free_tier=free_tier):
        logger.info("LLM option pick lacks a second source — queuing: %s", question[:50])
        return DraftResult()

    logger.info(
        "LLM selected option(s) for %s: %s",
        question[:50],
        value[:60],
    )
    # Used to fill the current application, but persist_policy does NOT save
    # "LLM-option" picks to memory — the best option can depend on the specific
    # job's option set, so it is re-selected per application.
    return DraftResult(fill=value, canonical=value, source="LLM-option", confidence=0.9)


def _option_pick_has_backing(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    value: str,
    free_tier: FreeTierContext,
) -> bool:
    """True when an independent source supports the LLM's option pick.

    Backing = a rule/RAG/config/prior-answer hint that resolves to the same
    option(s), or the verifier model approving the pick against profile + databank.
    """
    from .validation import answers_equivalent_for_agreement

    # 1) Hint agreement (cheap, no extra model call).
    hints: list[str] = []
    for hint in (free_tier.config_hint, free_tier.rag_fill, free_tier.rag_raw):
        if hint and str(hint).strip():
            hints.append(str(hint).strip())
    for sa in (free_tier.similar_answers or [])[:3]:
        ans = str(getattr(sa, "answer", "") or "").strip()
        if ans:
            hints.append(ans)
    for hint in hints:
        if answers_equivalent_for_agreement(question, field, hint, value, config):
            return True

    # 2) Verifier approval against profile + databank.
    from ..llm_answers import _build_application_context, verify_llm_answer_detailed

    _, verified = verify_llm_answer_detailed(
        config,
        question=question,
        field=field,
        fill_answer=value,
        profile_excerpt=_build_application_context(config),
        similar_answers=free_tier.similar_answers,
        rag_hint=free_tier.rag_fill or free_tier.rag_raw or free_tier.config_hint,
    )
    return verified


def _llm_draft_for_field(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    free_tier: FreeTierContext,
    profile_context: str | None = None,
) -> DraftResult:
    if not config.llm.enabled:
        hint = _rag_hint_from_context(free_tier)
        if hint:
            logger.info(
                "RAG hint available but LLM disabled — queuing: %s",
                question[:60],
            )
        return DraftResult()

    # Options-based questions: feed the options into the decision and return the
    # best option directly (no free-form generation + later conversion).
    if _is_choice_field(field):
        choice = _llm_select_choice(config, question=question, field=field, free_tier=free_tier)
        if choice.has_answer:
            return choice
        # fall through to free-form only if direct selection produced nothing

    from ..llm_answers import generate_llm_decision

    rag_hint = _rag_hint_from_context(free_tier)
    decision = generate_llm_decision(
        config,
        question=question,
        field=field,
        similar_answers=free_tier.similar_answers,
        rag_hint=rag_hint,
        free_tier=free_tier,
        profile_context=profile_context,
    )
    if not decision:
        return DraftResult()

    vector_best = free_tier.vector_best
    accepted, draft_source, effective_confidence = llm_decision_acceptable(
        config,
        decision,
        question=question,
        field=field,
        rag_hint=rag_hint,
        vector_hint=getattr(vector_best, "answer", None) if vector_best else None,
        vector_score=float(getattr(vector_best, "score", 0.0) or 0.0) if vector_best else 0.0,
        similar_answers=free_tier.similar_answers,
    )
    if accepted:
        # Persist on the corroboration-derived confidence, not the model's raw
        # self-reported number (see derive_effective_confidence).
        return DraftResult(
            fill=decision.answer,
            canonical=decision.canonical,
            source=draft_source,
            confidence=effective_confidence,
        )

    # Not accepted means no second source backed it (already logged in the policy).
    return DraftResult()


def _draft_one_field(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    jd: str = "",
    job_title: str = "",
    company: str = "",
    profile_context: str | None = None,
) -> DraftResult:
    """Rule RAG → vector top-1 → LLM (one cheap pre-pass, then at most one LLM call)."""
    field = enrich_field_for_llm(field)
    free_tier = collect_free_tier_context(
        config,
        question=question,
        field=field,
        job_title=job_title,
        jd=jd,
    )

    rule = _try_rule_rag_from_context(config, free_tier, question=question, field=field)
    if rule:
        return rule

    vector = try_vector_auto_answer(config, question, field, vector_best=free_tier.vector_best)
    if vector:
        fill, stored, source, confidence = vector
        return DraftResult(fill=fill, canonical=stored, source=source, confidence=confidence)

    return _llm_draft_for_field(
        config,
        question=question,
        field=field,
        free_tier=free_tier,
        profile_context=profile_context,
    )


def draft_answer_for_field(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    jd: str = "",
    job_title: str = "",
    company: str = "",
) -> DraftResult:
    """
    Rule RAG → vector top-1 → LLM → format finalize → validate.
    Returns DraftResult with fill/canonical/source/confidence.
    """
    field = enrich_field_for_llm(field)
    question = str(field.get("label", question) or question)
    cache_key = _draft_cache_key(question, field)
    with _draft_cache_lock:
        if cache_key in _draft_cache:
            return _draft_cache[cache_key]

    result = _draft_one_field(
        config,
        question=question,
        field=field,
        jd=jd,
        job_title=job_title,
        company=company,
    )
    return _cache_result(cache_key, result)


def draft_answers_for_fields(
    config: AppConfig,
    fields: list[tuple[str, dict[str, Any]]],
    *,
    jd: str = "",
    job_title: str = "",
    company: str = "",
) -> dict[str, DraftResult]:
    """Draft answers sequentially — no batch LLM (better accuracy)."""
    from ..llm_answers import _build_application_context

    results: dict[str, DraftResult] = {}
    profile_context = _build_application_context(config)

    for label, raw_field in fields:
        field = enrich_field_for_llm(raw_field)
        question = str(field.get("label", label) or label)
        cache_key = _draft_cache_key(question, field)
        with _draft_cache_lock:
            cached = _draft_cache.get(cache_key)
        if cached:
            results[question] = cached
            continue

        entry = _draft_one_field(
            config,
            question=question,
            field=field,
            jd=jd,
            job_title=job_title,
            company=company,
            profile_context=profile_context,
        )
        results[question] = entry
        if entry.has_answer:
            with _draft_cache_lock:
                _draft_cache[cache_key] = entry
    return results
