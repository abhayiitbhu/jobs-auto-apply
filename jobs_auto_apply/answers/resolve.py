from __future__ import annotations

import asyncio
import logging
from typing import Any

import click

from ..config import AppConfig
from .chips import is_chip_range_label
from .config_answers import authoritative_config_answer
from .draft import DraftResult, draft_answers_for_fields
from .experience import is_new_experience_question
from .fields import enrich_field_for_llm
from .interactive import prompt_confirm_new_answer
from .labels import interactive_prompt_lock
from .memory_store import (
    build_answer_group_index,
    canonicalize_stored_answer,
    flag_rejected_saved_answer,
    get_saved_answer,
    persist_answer,
    resolve_fill_answer,
    save_answer,
)
from .persist_policy import (
    draft_should_persist_to_memory,
    effective_persist_threshold,
    rag_rule_persist_confidence,
)
from .validation import answer_acceptable_for_field, is_placeholder_answer

logger = logging.getLogger("job_apply")


def _fill_from_draft(
    label: str,
    field: dict[str, Any],
    draft: DraftResult,
    config: AppConfig,
) -> str:
    stored = draft.canonical or canonicalize_stored_answer(label, draft.fill or "", field, config)
    return resolve_fill_answer(stored, field, config)


def _try_persist_draft(
    config: AppConfig,
    *,
    label: str,
    field: dict[str, Any],
    draft: DraftResult,
    company: str,
    job_title: str,
) -> bool:
    stored = draft.canonical or canonicalize_stored_answer(label, draft.fill or "", field, config)
    if not draft_should_persist_to_memory(
        config,
        question=label,
        field=field,
        source=draft.source or "LLM",
        confidence=draft.confidence,
        answer=stored,
    ):
        logger.info(
            "Ephemeral %s answer (confidence=%.2f, not saved): %s",
            draft.source or "auto",
            draft.confidence,
            label[:60],
        )
        return False
    _, _, saved = persist_answer(
        config.base_dir,
        label,
        draft.fill or "",
        field,
        config,
        company=company,
        job_title=job_title,
        canonical=draft.canonical,
        reviewed=True,
        source=draft.source or "LLM",
    )
    if saved:
        logger.info(
            "Saved %s answer (confidence=%.2f): %s",
            draft.source or "LLM",
            draft.confidence,
            label[:60],
        )
    return saved


def _draft_is_fillable(
    config: AppConfig,
    *,
    label: str,
    field: dict[str, Any],
    draft: DraftResult,
) -> bool:
    """Only auto-fill an answer that clears the persist confidence bar.

    A corroborated answer that isn't confident enough to *save* is also not
    confident enough to *auto-fill* — queue it for manual input instead.
    Exceptions: option picks and config/facts answers are trustworthy but
    intentionally ephemeral (re-derived/re-selected per job), so they always fill.
    This bar is independent of the ``auto_save`` toggle (which only governs
    whether we write to memory, not whether we fill).
    """
    from .validation import requires_personal_artifact

    # Prompts that demand a personal artifact (paste a GitHub/demo link, give an
    # example of something you built) can never be truthfully auto-filled — always
    # queue them for manual answer, even if a draft cleared the confidence bar.
    if requires_personal_artifact(label):
        return False
    src = (draft.source or "").strip()
    if src in ("LLM-option", "config"):
        return True
    threshold = effective_persist_threshold(config, label)
    if src == "RAG":
        stored = draft.canonical or canonicalize_stored_answer(label, draft.fill or "", field, config)
        return rag_rule_persist_confidence(config, question=label, field=field, answer=stored) >= threshold
    return draft.confidence >= threshold


async def resolve_question_answers(
    config: AppConfig,
    job: Any,
    jd: str,
    questions: list[dict[str, Any]],
    *,
    interactive: bool | None = None,
    confirm_new: bool | None = None,
    defer_new: bool = False,
) -> dict[str, str]:
    """Saved answers → RAG rules → LLM → confirm / defer / interactive prompt."""
    answers: dict[str, str] = {}
    use_interactive = config.application.interactive_questions if interactive is None else interactive
    use_confirm_new = config.application.confirm_new_answers if confirm_new is None else confirm_new
    job_title = getattr(job, "title", "") or ""
    company = getattr(job, "company", "") or ""

    needs_draft: list[tuple[str, dict[str, Any]]] = []

    from ..memory import load_memory

    memory = load_memory(config.base_dir)
    answers_store = memory.get("question_answers", {})
    group_index = build_answer_group_index(answers_store)

    for raw_field in questions:
        field = enrich_field_for_llm(raw_field)
        label = field["label"]
        saved = get_saved_answer(
            config.base_dir,
            label,
            field,
            config=config,
            answers=answers_store,
            group_index=group_index,
        )
        if saved and is_chip_range_label(saved):
            saved = canonicalize_stored_answer(label, saved, field, config)
            save_answer(
                config.base_dir,
                label,
                saved,
                company=company,
                job_title=job_title,
            )
        if saved and not is_placeholder_answer(saved):
            # get_saved_answer already validates fit (group matches) and honors
            # user-reviewed answers (exact matches), so trust its output here
            # rather than re-running heuristics that would re-reject a valid
            # human answer and re-queue the same question every run.
            fill_value = resolve_fill_answer(saved, field, config)
            answers[label] = fill_value or saved
            logger.info("Using saved answer for: %s", label[:60])
            continue
        config_answer = authoritative_config_answer(config, label, field)
        if (
            config_answer
            and not is_placeholder_answer(config_answer)
            and answer_acceptable_for_field(label, config_answer, field)
        ):
            fill_value = resolve_fill_answer(config_answer, field, config)
            answers[label] = fill_value
            logger.info("Using config answer for: %s", label[:60])
            continue
        if saved and not is_placeholder_answer(saved):
            flag_rejected_saved_answer(config.base_dir, label, saved)
            logger.info(
                "Saved answer not valid for %s field; regenerating: %s",
                field.get("kind", "text"),
                label[:60],
            )
        needs_draft.append((label, field))

    draft_map = await asyncio.to_thread(
        draft_answers_for_fields,
        config,
        needs_draft,
        jd=jd,
        job_title=job_title,
        company=company,
    )

    for label, field in needs_draft:
        draft = draft_map.get(label, DraftResult())

        if defer_new:
            if draft.has_answer and is_new_experience_question(config, label):
                logger.info(
                    "New experience draft (%s, confidence=%.2f): %s",
                    draft.source or "LLM",
                    draft.confidence,
                    label[:60],
                )
            if draft.has_answer:
                fill_value = _fill_from_draft(label, field, draft, config)
                if (
                    fill_value
                    and answer_acceptable_for_field(label, fill_value, field)
                    and _draft_is_fillable(config, label=label, field=field, draft=draft)
                ):
                    answers[label] = fill_value
                    _try_persist_draft(
                        config,
                        label=label,
                        field=field,
                        draft=draft,
                        company=company,
                        job_title=job_title,
                    )
                else:
                    logger.warning(
                        "%s draft not usable / below persist bar for field: %s",
                        draft.source or "Auto",
                        label[:60],
                    )
            else:
                config_answer = authoritative_config_answer(config, label, field)
                if config_answer and answer_acceptable_for_field(label, config_answer, field):
                    stored, fill_value, saved = persist_answer(
                        config.base_dir,
                        label,
                        config_answer,
                        field,
                        config,
                        company=company,
                        job_title=job_title,
                        reviewed=True,
                        source="config",
                    )
                    if fill_value:
                        answers[label] = fill_value
                    if saved:
                        logger.info("Using config answer for: %s", label[:60])
                elif is_new_experience_question(config, label):
                    logger.info(
                        "Deferred new experience question (will queue): %s",
                        label[:60],
                    )
                else:
                    logger.info("Deferred question (will queue): %s", label[:60])
            continue

        if use_confirm_new:
            async with interactive_prompt_lock():
                confirmed = prompt_confirm_new_answer(
                    label,
                    field,
                    draft.fill,
                    job_title=job_title,
                    company=company,
                )
            if not confirmed:
                logger.info("Skipped new question: %s", label[:60])
                continue
            stored, fill_value, saved = persist_answer(
                config.base_dir,
                label,
                confirmed,
                field,
                config,
                company=company,
                job_title=job_title,
                reviewed=True,
                source="confirmed",
            )
            if not saved:
                logger.warning("Could not save confirmed answer for: %s", label[:60])
                continue
            answers[label] = fill_value
            logger.info("Confirmed answer for: %s", label[:60])
            continue

        if draft.has_answer:
            fill_value = _fill_from_draft(label, field, draft, config)
            if not fill_value or not answer_acceptable_for_field(label, fill_value, field):
                logger.warning(
                    "%s answer not usable for field: %s",
                    draft.source or "Auto",
                    label[:60],
                )
                continue
            if not _draft_is_fillable(config, label=label, field=field, draft=draft):
                logger.info(
                    "%s answer below persist bar (confidence=%.2f) — queuing: %s",
                    draft.source or "Auto",
                    draft.confidence,
                    label[:60],
                )
                continue
            answers[label] = fill_value
            if _try_persist_draft(
                config,
                label=label,
                field=field,
                draft=draft,
                company=company,
                job_title=job_title,
            ):
                logger.info("%s answer for: %s", draft.source or "Auto", label[:60])
            continue

        if not use_interactive:
            logger.info("No answer for: %s", label[:60])
            continue

        async with interactive_prompt_lock():
            click.echo(f"\nApplication question: {label}")
            entered = click.prompt("Your answer", default="").strip()
        if not entered:
            continue

        _stored, fill_value, saved = persist_answer(
            config.base_dir,
            label,
            entered,
            field,
            config,
            company=company,
            job_title=job_title,
            reviewed=True,
            source="interactive",
        )
        if not saved:
            logger.warning("Could not save interactive answer for: %s", label[:60])
            continue
        answers[label] = fill_value
    return answers
