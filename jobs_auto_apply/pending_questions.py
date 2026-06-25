from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from .application_questions import (
    _infer_field_for_question,
    draft_answer_for_field,
    enrich_field_for_llm,
    get_saved_answer,
    infer_field_input_type,
    is_chip_range_label,
    is_generic_question_label,
    is_plausible_application_question,
    is_skill_years_question,
    needs_review_answer,
    parse_years_numeric_value,
    persist_answer,
    question_key,
    resolve_fill_answer,
    save_answer,
)
from .answers.compensation import is_numeric_ctc_question
from .answers.memory_store import memory_key, sanitize_user_answer
from .answers.validation import answer_acceptable_for_field
from .config import AppConfig
from .memory import load_memory
from .question_groups import PendingQuestionGroup, classify_question, group_pending_entries

from .pending_job_ref import PendingJobRef

logger = logging.getLogger("job_apply")

_lock = threading.Lock()


def pending_questions_path(base_dir: Path, config: AppConfig | None = None) -> Path:
    if config is not None:
        return config.pending_questions_path
    return base_dir / "data" / "pending_questions.json"


def _load(base_dir: Path, config: AppConfig | None = None) -> dict[str, Any]:
    path = pending_questions_path(base_dir, config)
    if not path.exists():
        return {"questions": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(
    base_dir: Path, data: dict[str, Any], config: AppConfig | None = None
) -> None:
    path = pending_questions_path(base_dir, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _pending_field_meta(field: dict[str, Any] | None) -> dict[str, Any]:
    if not field:
        return {}
    keep = ("kind", "input_type", "input_mode", "options", "platform", "placeholder")
    return {k: field[k] for k in keep if k in field and field[k]}


def _field_for_pending_entry(
    label: str, entry: dict[str, Any], config: AppConfig | None
) -> dict[str, Any]:
    stored = entry.get("field")
    if isinstance(stored, dict) and stored:
        field = dict(stored)
        field.setdefault("label", label)
        field.pop("input_type", None)
        return enrich_field_for_llm(field)
    return enrich_field_for_llm(_infer_field_for_question(label, config))


def _memory_has_user_answer(
    base_dir: Path,
    label: str,
    field: dict[str, Any],
    config: AppConfig | None,
) -> bool:
    saved = get_saved_answer(base_dir, label, field, config=config)
    if saved and not is_chip_range_label(saved):
        if answer_acceptable_for_field(label, saved, field):
            return True
        fill = resolve_fill_answer(saved, field, config)
        if fill and answer_acceptable_for_field(label, fill, field):
            return True
    entry = load_memory(base_dir, config).get("question_answers", {}).get(
        memory_key(label)
    )
    if not isinstance(entry, dict) or entry.get("needs_review"):
        return False
    ans = str(entry.get("answer", "")).strip()
    if not ans:
        return False
    if entry.get("reviewed") and str(entry.get("source", "")) in (
        "manual",
        "pending",
        "confirmed",
        "interactive",
    ):
        return True
    return False


def _coerce_pending_user_answer(
    label: str,
    user_answer: str,
    field: dict[str, Any],
    config: AppConfig | None,
) -> str:
    """Map free-text pending answers to the shape each field expects."""
    text = sanitize_user_answer(user_answer)
    if not text:
        return text
    field = enrich_field_for_llm({**field, "label": label})
    input_type = infer_field_input_type(label, field)
    kind = str(field.get("kind", "text"))
    options = [str(o).strip() for o in (field.get("options") or []) if str(o).strip()]
    opts_lower = {o.lower() for o in options}

    years = parse_years_numeric_value(text)
    if years is not None and re.search(r"\byears?\b", text, re.I):
        return str(int(years)) if years == int(years) else str(years)

    wants_years = (
        input_type == "years_numeric"
        or is_skill_years_question(label)
        or (
            re.search(r"\bhow (many|much)\b.*\b(years?|experience)\b", label, re.I)
            and not opts_lower <= {"yes", "no"}
        )
        or (
            years is not None
            and re.search(
                r"\b(do you have|experience).*(genai|llm|ai/ml|copilot|dataiku)\b",
                label,
                re.I,
            )
        )
    )
    if years is not None and wants_years:
        return str(int(years)) if years == int(years) else str(years)

    if is_numeric_ctc_question(label) or input_type == "ctc_numeric":
        num = re.search(r"(\d+(?:\.\d+)?)", text)
        if num:
            return num.group(1)

    if kind == "radio" or (options and opts_lower <= {"yes", "no"}):
        low = text.lower()
        if low in ("yes", "y", "true", "1") or re.match(r"^yes\b", low):
            return next((o for o in options if o.lower() == "yes"), "Yes")
        if low in ("no", "n", "false", "0") or re.match(r"^no\b", low):
            return next((o for o in options if o.lower() == "no"), "No")
        if years is not None:
            return "Yes" if years > 0 else "No"

    if config:
        from .answers.memory_store import canonicalize_stored_answer

        return canonicalize_stored_answer(label, text, field, config)
    return text


def _group_has_saved_answer(
    base_dir: Path,
    group_id: str,
    config: AppConfig | None = None,
) -> bool:
    if group_id.startswith("unique:"):
        return False
    memory = load_memory(base_dir, config)
    for entry in memory.get("question_answers", {}).values():
        if not isinstance(entry, dict):
            continue
        stored_q = str(entry.get("question", "")).strip()
        ans = str(entry.get("answer", "")).strip()
        if not stored_q or not ans or is_chip_range_label(ans):
            continue
        if entry.get("needs_review"):
            continue
        if classify_question(stored_q) != group_id:
            continue
        field = enrich_field_for_llm(_infer_field_for_question(stored_q, config))
        if answer_acceptable_for_field(stored_q, ans, field):
            return True
        fill = resolve_fill_answer(ans, field, config)
        if fill and answer_acceptable_for_field(stored_q, fill, field):
            return True
    return False


def _save_pending_group_answers(
    base_dir: Path,
    group: PendingQuestionGroup,
    user_answer: str,
    config: AppConfig | None,
    *,
    company: str = "",
    job_title: str = "",
) -> list[str]:
    """Save one user answer to every variant in the group; returns stored values."""
    stored_values: list[str] = []
    pending_data = _load(base_dir, config)
    pending_questions = pending_data.get("questions", {})

    for variant in group.variants:
        entry = pending_questions.get(question_key(variant), {})
        field = _field_for_pending_entry(
            variant, entry if isinstance(entry, dict) else {}, config
        )
        stored = _coerce_pending_user_answer(variant, user_answer, field, config)
        if not config:
            save_answer(
                base_dir,
                variant,
                stored,
                company=company,
                job_title=job_title,
                reviewed=True,
                source="manual",
            )
            stored_values.append(stored)
            continue
        canonical, fill, saved = persist_answer(
            base_dir,
            variant,
            stored,
            field,
            config,
            company=company,
            job_title=job_title,
            canonical=stored,
            reviewed=True,
            source="manual",
        )
        if saved:
            stored_values.append(canonical)
    return stored_values


def queue_unanswered(
    base_dir: Path,
    *,
    source: str,
    job_title: str,
    company: str,
    job_url: str,
    labels: list[str],
    fields_by_label: dict[str, dict[str, Any]] | None = None,
) -> int:
    """Record questions we could not answer during apply. Returns newly queued count."""
    if not labels:
        return 0
    fields_by_label = fields_by_label or {}
    with _lock:
        data = _load(base_dir)
        questions = data.setdefault("questions", {})
        added = 0
        now = datetime.now(timezone.utc).isoformat()
        for label in labels:
            label = label.strip()
            if not label:
                continue
            field = fields_by_label.get(label)
            # A field means this came straight from a live form's DOM (discovered
            # during apply), so it is a real question even if its wording fails the
            # scraped-chrome plausibility heuristic (e.g. "Total Exp", "Kindly
            # mention…", "Rate yourself…"). Trust it; only the plausibility gate is
            # relaxed for these — saved/group checks still apply.
            has_dom_field = bool(field)
            if get_saved_answer(base_dir, label, field, config=None):
                continue
            group_id = classify_question(label)
            if _group_has_saved_answer(base_dir, group_id):
                continue
            if is_generic_question_label(label):
                logger.debug("Skipping generic placeholder question: %s", label)
                continue
            if not has_dom_field and not is_plausible_application_question(label):
                logger.debug("Skipping non-question label: %s", label[:80])
                continue
            key = question_key(label)
            entry = questions.setdefault(
                key,
                {"question": label, "key": key, "jobs": []},
            )
            meta = _pending_field_meta(field)
            if meta:
                entry["field"] = meta
            seen_urls = {j.get("url") for j in entry.get("jobs", [])}
            if job_url not in seen_urls:
                entry.setdefault("jobs", []).append(
                    {
                        "source": source,
                        "title": job_title,
                        "company": company,
                        "url": job_url,
                        "at": now,
                    }
                )
                added += 1
        _save(base_dir, data)
    if added:
        logger.info("Queued %d unanswered question(s) for %s", len(labels), job_title)
    return added


def pending_question_list(base_dir: Path, config: AppConfig | None = None) -> list[dict[str, Any]]:
    """Unique questions still missing answers in user_memory."""
    data = _load(base_dir, config)
    pending: list[dict[str, Any]] = []
    for entry in data.get("questions", {}).values():
        label = str(entry.get("question", "")).strip()
        if not label:
            continue
        field = _field_for_pending_entry(
            label, entry if isinstance(entry, dict) else {}, config
        )
        if _memory_has_user_answer(base_dir, label, field, config):
            continue
        if _group_has_saved_answer(base_dir, classify_question(label), config):
            continue
        pending.append(entry)
    pending.sort(key=lambda e: len(e.get("jobs", [])), reverse=True)
    return pending


def pending_groups(base_dir: Path, config: AppConfig | None = None) -> list[PendingQuestionGroup]:
    return group_pending_entries(pending_question_list(base_dir, config))


def pending_count(base_dir: Path, config: AppConfig | None = None) -> int:
    prune_answered(base_dir, config)
    return len(pending_question_list(base_dir, config))


def remove_answered(base_dir: Path, label: str) -> None:
    group_id = classify_question(label)
    _remove_group(base_dir, group_id)


def _remove_group(base_dir: Path, group_id: str) -> None:
    with _lock:
        data = _load(base_dir)
        questions = data.get("questions", {})
        for key in list(questions.keys()):
            label = str(questions[key].get("question", ""))
            if label and classify_question(label) == group_id:
                del questions[key]
        _save(base_dir, data)


def prune_answered(base_dir: Path, config: AppConfig | None = None) -> int:
    """Drop pending entries that now have saved answers. Returns count removed."""
    with _lock:
        data = _load(base_dir, config)
        questions = data.get("questions", {})
        before = len(questions)
        for key in list(questions.keys()):
            label = str(questions[key].get("question", ""))
            # Keep DOM-discovered questions (those with stored field metadata) even
            # if their wording fails the plausibility heuristic — they were real
            # live form fields, not scraped chrome.
            has_dom_field = bool(
                isinstance(questions[key], dict) and questions[key].get("field")
            )
            if is_generic_question_label(label) or (
                not has_dom_field and not is_plausible_application_question(label)
            ):
                del questions[key]
                continue
            field = _field_for_pending_entry(
                label, questions[key], config
            )
            if _memory_has_user_answer(base_dir, label, field, config):
                del questions[key]
                continue
            if _group_has_saved_answer(base_dir, classify_question(label), config):
                del questions[key]
        _save(base_dir, data, config)
        return before - len(questions)


def _print_group_context(group: PendingQuestionGroup) -> None:
    if len(group.variants) > 1:
        click.echo("\nApplies to all of these wordings:")
        for variant in group.variants:
            click.echo(f"  • {variant}")
    elif group.variants and group.variants[0] != group.title:
        click.echo(f"\n{group.variants[0]}")

    if group.hint:
        click.echo(f"\nTip: {group.hint}")

    if group.jobs:
        click.echo("\nSeen on:")
        for job in group.jobs[:5]:
            title = job.get("title", "?")
            company = job.get("company") or "?"
            click.echo(f"  • {title} @ {company}")
        if len(group.jobs) > 5:
            click.echo(f"  … and {len(group.jobs) - 5} more job(s)")


def _prompt_review_answer(
    *,
    index: int,
    total: int,
    question: str,
    current_answer: str | None = None,
    job_title: str = "",
    company: str = "",
    suggested_answer: str | None = None,
) -> str | None:
    """
    Show one question and wait for user decision.
    Returns new answer text, None to keep current, or empty string to skip.
    """
    click.echo(f"\n{'─' * 60}")
    click.echo(f"Question {index} of {total}")
    click.echo(f"{'─' * 60}")
    click.echo(question)
    if job_title:
        where = f"{job_title} @ {company}" if company else job_title
        click.echo(f"\nSeen on: {where}")
    if current_answer:
        click.echo(f"\nCurrent answer:\n{current_answer}")
    if suggested_answer and suggested_answer.strip() != (current_answer or "").strip():
        click.echo(f"\nSuggested (from resume + LLM):\n{suggested_answer}")

    while True:
        if current_answer:
            prompt = "Action — (k)eep  (e)dit  (s)kip"
            if suggested_answer:
                prompt += "  (a)ccept suggested"
            default = "a" if suggested_answer else "k"
        else:
            prompt = "Action — (e)nter answer  (s)kip"
            if suggested_answer:
                prompt += "  (a)ccept suggested"
            default = "a" if suggested_answer else "e"

        action = click.prompt(f"\n{prompt}", default=default).lower().strip()

        if action in ("a", "accept", "suggested") and suggested_answer:
            click.echo("Accepted suggested answer.")
            return suggested_answer

        if action in ("k", "keep") and current_answer:
            click.echo("Kept.")
            return None

        if action in ("s", "skip"):
            click.echo("Skipped.")
            return ""

        if action in ("e", "edit", "enter", "a", "answer"):
            default_text = current_answer or ""
            new = click.prompt(
                "Your answer",
                default=default_text,
                show_default=bool(default_text),
            ).strip()
            if new:
                click.echo("Saved.")
                return new
            click.echo("Answer cannot be empty. Choose (e)dit again, (k)eep, or (s)kip.")
            continue

        click.echo("Invalid choice. Use k, e, or s.")


def answer_pending_groups_interactive(
    base_dir: Path,
    config: AppConfig | None = None,
    *,
    prompt_on_failure: bool = True,
    suggest_answers: bool = False,
) -> tuple[int, list[PendingJobRef]]:
    """
    Prompt once per question group; save to user_memory.json for all variants.
    When LLM auto_answer_pending is enabled, fills answers without prompting.
    When suggest_answers is True (or auto), drafts via LLM before each prompt.
    Returns (answered_count, jobs_to_retry_live).
    """
    prune_answered(base_dir, config)
    groups = pending_groups(base_dir, config)
    if not groups:
        click.echo("No pending application questions.")
        review_n = _answers_for_review(base_dir, all_answers=False)
        if review_n:
            click.echo(
                f"\n{len(review_n)} saved answer(s) look auto-generated/wrong. "
                "Run: python3 main.py answer-questions --review"
            )
        return 0, []

    total = len(groups)
    auto_pending = bool(config and config.llm.enabled and config.llm.auto_answer_pending)
    use_llm_draft = bool(config and config.llm.enabled and (auto_pending or suggest_answers))
    jobs_to_retry: list[PendingJobRef] = []

    # Non-interactive deferral: when prompting is disabled (prompt_on_failure=False)
    # and we can't auto-answer (LLM off / auto_answer_pending false), leave every
    # group in pending and move on instead of blocking the run. The user resolves
    # them later via `answer-questions`. Without this, the loop below would prompt
    # regardless because of the `not auto_pending` branch.
    if not auto_pending and not prompt_on_failure:
        click.echo(
            f"\n{total} pending question group(s) deferred — "
            "run: python3 main.py answer-questions to resolve."
        )
        return 0, []

    if auto_pending:
        click.echo(f"\n{'=' * 60}")
        click.echo(f"  Auto-answering {total} pending question group(s) (RAG → LLM)")
        click.echo(f"{'=' * 60}")
    else:
        click.echo(f"\n{'=' * 60}")
        click.echo(f"  {total} question group(s) to answer")
        click.echo("  Similar questions are grouped — one answer applies to all variants.")
        if suggest_answers:
            click.echo("  LLM may suggest an answer — you confirm before it is saved.")
        else:
            click.echo("  Type your answer for each group (no LLM wait between questions).")
        click.echo(f"{'=' * 60}")

    answered = 0
    for index, group in enumerate(groups, 1):
        example = group.jobs[0] if group.jobs else {}
        primary = group.variants[0]
        job_title = str(example.get("title", ""))
        company = str(example.get("company", ""))
        pending_data = _load(base_dir, config)
        primary_entry = pending_data.get("questions", {}).get(question_key(primary), {})
        field = _field_for_pending_entry(
            primary,
            primary_entry if isinstance(primary_entry, dict) else {},
            config,
        )

        new_answer = ""
        stored_canonical = ""
        draft_source = ""
        draft = None
        if use_llm_draft:
            draft_result = draft_answer_for_field(
                config,
                question=primary,
                field=field,
                job_title=job_title,
                company=company,
            )
            draft = draft_result.fill
            stored_canonical = draft_result.canonical or ""
            draft_source = draft_result.source

        if auto_pending and draft:
            new_answer = draft
            stored_canonical = stored_canonical or ""
            click.echo(f"\n[{index}/{total}] {primary[:70]}")
            click.echo(f"  → {new_answer[:120]} ({draft_source or 'LLM'})")
        elif prompt_on_failure:
            click.echo(f"\n{'─' * 60}")
            click.echo(f"Group {index} of {total}: {group.title}")
            click.echo(f"{'─' * 60}")
            _print_group_context(group)
            input_type = str(field.get("input_type") or infer_field_input_type(primary, field))
            options = field.get("options") or []
            if input_type == "years_numeric":
                click.echo("\nTip: enter years as a number (e.g. 2, 4, or 0 for none).")
            elif input_type == "ctc_numeric":
                click.echo("\nTip: enter CTC in LPA as a number (e.g. 38).")
            elif options:
                click.echo(f"\nLikely options: {', '.join(options)}")
            if draft:
                click.echo(
                    f"\nSuggested ({draft_source or 'LLM'}): {draft[:120]}"
                    + ("…" if len(draft) > 120 else "")
                )
                if click.confirm("Use this answer?", default=False):
                    new_answer = draft
                else:
                    new_answer = click.prompt(
                        "Your answer (Enter to skip)",
                        default="",
                        show_default=False,
                    ).strip()
            else:
                new_answer = click.prompt(
                    "\nYour answer (Enter to skip)",
                    default="",
                    show_default=False,
                ).strip()
            if not new_answer:
                click.echo("Skipped.")
                continue

        if not new_answer:
            if auto_pending:
                click.echo(f"  Skipped (no answer): {primary[:60]}")
            continue

        aliases = group.variants[1:] if len(group.variants) > 1 else None
        saved = False
        canonical = new_answer.strip()
        if config:
            stored_values = _save_pending_group_answers(
                base_dir,
                group,
                new_answer,
                config,
                company=company,
                job_title=job_title,
            )
            saved = bool(stored_values)
            canonical = stored_values[0] if stored_values else canonical
        else:
            save_answer(
                base_dir,
                primary,
                new_answer,
                company=company,
                job_title=job_title,
                aliases=aliases,
                reviewed=True,
                source="manual",
            )
            saved = True

        if not saved:
            click.echo(
                "  [red]Not saved[/red] — answer failed validation. "
                "Try a fuller answer or run with --suggest."
            )
            continue

        _remove_group(base_dir, group.group_id)
        answered += 1
        click.echo(
            f"  Saved to user_memory.json ({canonical[:60]}"
            f"{'…' if len(canonical) > 60 else ''})."
        )
        for job in group.jobs:
            url = str(job.get("url", "")).strip()
            source = str(job.get("source", "")).strip()
            if not url or source not in ("naukri", "hirist"):
                continue
            jobs_to_retry.append(
                PendingJobRef(
                    source=source,
                    title=str(job.get("title", "")),
                    company=str(job.get("company", "")),
                    url=url,
                )
            )

    if answered and jobs_to_retry and config and config.llm.retry_pending_jobs:
        click.echo(
            f"\n[OK] Saved {answered} answer group(s). Retrying {len(_dedupe_retry_jobs(jobs_to_retry))} job(s) live…\n"
        )
    else:
        click.echo(f"\n[OK] Saved {answered} answer group(s).\n")

    remaining = len(pending_groups(base_dir, config))
    if remaining:
        click.echo(
            f"[yellow]{remaining} question group(s) still pending.[/yellow] "
            "Run: [bold]python3 main.py answer-questions[/bold]"
        )
    return answered, jobs_to_retry


def _dedupe_retry_jobs(refs: list[PendingJobRef]) -> list[PendingJobRef]:
    seen: set[str] = set()
    out: list[PendingJobRef] = []
    for ref in refs:
        if ref.url not in seen:
            seen.add(ref.url)
            out.append(ref)
    return out


def answer_pending_interactive(base_dir: Path, config: AppConfig | None = None) -> int:
    """Prompt for pending questions grouped by topic."""
    answered, _jobs = answer_pending_groups_interactive(base_dir, config=config)
    return answered


def summary_for_run(
    base_dir: Path, *, platform: str | None = None, config: AppConfig | None = None
) -> str:
    prune_answered(base_dir, config)
    pending_n = len(pending_question_list(base_dir, config))
    review_n = saved_answers_needing_review_count(base_dir)
    if not pending_n and not review_n:
        return ""
    lines: list[str] = []
    if pending_n:
        group_n = len(pending_groups(base_dir, config))
        lines.append(
            f"[yellow]{pending_n} question(s) in {group_n} group(s) need answers.[/yellow]"
        )
    if review_n:
        lines.append(f"[yellow]{review_n} saved answer(s) need your review.[/yellow]")
        lines.append("Run: [bold]python3 main.py answer-questions --review[/bold]")
    rerun = (
        f"python3 main.py run --platform {platform}"
        if platform and platform != "all"
        else "python3 main.py run"
    )
    if pending_n:
        lines.append(f"Then re-run: [bold]{rerun}[/bold]")
    return "\n" + "\n".join(lines) + "\n"


def _saved_answer_entries(base_dir: Path) -> list[dict[str, Any]]:
    data = load_memory(base_dir)
    entries: list[dict[str, Any]] = []
    for key, entry in data.get("question_answers", {}).items():
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("question", "")).strip()
        answer = str(entry.get("answer", "")).strip()
        if not label:
            continue
        entries.append(
            {
                "key": key,
                "question": label,
                "answer": answer,
                "company": str(entry.get("company", "")),
                "job_title": str(entry.get("job_title", "")),
            }
        )
    return entries


def saved_answers_needing_review_count(base_dir: Path) -> int:
    return len(_answers_for_review(base_dir, all_answers=False))


def _answers_for_review(base_dir: Path, *, all_answers: bool) -> list[dict[str, Any]]:
    entries = _saved_answer_entries(base_dir)
    if not all_answers:
        entries = [
            e for e in entries if needs_review_answer(e["question"], e["answer"])
        ]
    entries.sort(key=lambda e: e["question"].lower())
    return entries


def review_saved_answers_interactive(
    base_dir: Path,
    *,
    all_answers: bool = False,
    config: AppConfig | None = None,
) -> int:
    """Review each saved answer one at a time; user must keep, edit, or skip before next."""
    entries = _answers_for_review(base_dir, all_answers=all_answers)
    if not entries:
        label = "saved" if all_answers else "bad"
        click.echo(f"No {label} answers to review.")
        return 0

    mode = "all saved" if all_answers else "needs review"
    total = len(entries)
    click.echo(f"\n{'=' * 60}")
    click.echo(f"  {total} answer(s) to review ({mode})")
    if config and config.llm.enabled:
        click.echo("  Bad answers get a resume-based suggestion — (a)ccept suggested when shown.")
    click.echo("  Review each one — (k)eep, (e)dit, or (s)kip — then move to next.")
    click.echo(f"{'=' * 60}")

    updated = 0
    for index, entry in enumerate(entries, 1):
        suggested = None
        if config and (all_answers or needs_review_answer(entry["question"], entry["answer"])):
            from .answer_suggest import suggest_answer

            suggested = suggest_answer(
                config,
                question=entry["question"],
                job_title=entry.get("job_title", ""),
                company=entry.get("company", ""),
            )

        new_answer = _prompt_review_answer(
            index=index,
            total=total,
            question=entry["question"],
            current_answer=entry["answer"],
            job_title=entry.get("job_title", ""),
            company=entry.get("company", ""),
            suggested_answer=suggested,
        )
        if new_answer is None:
            continue
        if not new_answer:
            continue
        save_answer(
            base_dir,
            entry["question"],
            new_answer,
            company=entry.get("company", ""),
            job_title=entry.get("job_title", ""),
            reviewed=True,
            source="manual",
            config=config,
        )
        updated += 1

    click.echo(f"\n[OK] Updated {updated} answer(s).\n")
    return updated
