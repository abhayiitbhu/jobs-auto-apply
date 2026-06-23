from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from .application_questions import (
    _infer_field_for_question,
    draft_answer_for_field,
    get_saved_answer,
    is_chip_range_label,
    is_generic_question_label,
    is_plausible_application_question,
    needs_review_answer,
    persist_answer,
    question_key,
    resolve_fill_answer,
    save_answer,
)
from .config import AppConfig
from .memory import load_memory
from .question_groups import PendingQuestionGroup, classify_question, group_pending_entries

from .pending_job_ref import PendingJobRef

logger = logging.getLogger("job_apply")

_lock = threading.Lock()


def pending_questions_path(base_dir: Path) -> Path:
    return base_dir / "data" / "pending_questions.json"


def _load(base_dir: Path) -> dict[str, Any]:
    path = pending_questions_path(base_dir)
    if not path.exists():
        return {"questions": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(base_dir: Path, data: dict[str, Any]) -> None:
    path = pending_questions_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
            if get_saved_answer(base_dir, label, field):
                continue
            if is_generic_question_label(label):
                logger.debug("Skipping generic placeholder question: %s", label)
                continue
            if not is_plausible_application_question(label):
                logger.debug("Skipping non-question label: %s", label[:80])
                continue
            key = question_key(label)
            entry = questions.setdefault(
                key,
                {"question": label, "key": key, "jobs": []},
            )
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


def pending_question_list(base_dir: Path) -> list[dict[str, Any]]:
    """Unique questions still missing answers in user_memory."""
    data = _load(base_dir)
    pending: list[dict[str, Any]] = []
    for entry in data.get("questions", {}).values():
        label = str(entry.get("question", "")).strip()
        saved = get_saved_answer(base_dir, label) if label else None
        if not label or (saved and not is_chip_range_label(saved)):
            continue
        pending.append(entry)
    pending.sort(key=lambda e: len(e.get("jobs", [])), reverse=True)
    return pending


def pending_groups(base_dir: Path) -> list[PendingQuestionGroup]:
    return group_pending_entries(pending_question_list(base_dir))


def pending_count(base_dir: Path) -> int:
    prune_answered(base_dir)
    return len(pending_question_list(base_dir))


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


def prune_answered(base_dir: Path) -> int:
    """Drop pending entries that now have saved answers. Returns count removed."""
    with _lock:
        data = _load(base_dir)
        questions = data.get("questions", {})
        before = len(questions)
        for key in list(questions.keys()):
            label = str(questions[key].get("question", ""))
            if is_generic_question_label(label) or not is_plausible_application_question(label):
                del questions[key]
                continue
            if label and get_saved_answer(base_dir, label):
                saved = get_saved_answer(base_dir, label)
                if saved and not is_chip_range_label(saved):
                    del questions[key]
                    continue
        _save(base_dir, data)
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
) -> tuple[int, list[PendingJobRef]]:
    """
    Prompt once per question group; save to user_memory.json for all variants.
    When LLM auto_answer_pending is enabled, fills answers without prompting.
    Returns (answered_count, jobs_to_retry_live).
    """
    prune_answered(base_dir)
    groups = pending_groups(base_dir)
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
    jobs_to_retry: list[PendingJobRef] = []

    if auto_pending:
        click.echo(f"\n{'=' * 60}")
        click.echo(f"  Auto-answering {total} pending question group(s) (RAG → LLM)")
        click.echo(f"{'=' * 60}")
    else:
        click.echo(f"\n{'=' * 60}")
        click.echo(f"  {total} question group(s) to answer")
        click.echo("  Similar questions are grouped — one answer applies to all variants.")
        click.echo(f"{'=' * 60}")

    answered = 0
    for index, group in enumerate(groups, 1):
        example = group.jobs[0] if group.jobs else {}
        primary = group.variants[0]
        job_title = str(example.get("title", ""))
        company = str(example.get("company", ""))

        new_answer = ""
        stored_canonical = ""
        if auto_pending and config:
            field = _infer_field_for_question(primary)
            draft, stored_canonical, draft_source = draft_answer_for_field(
                config,
                question=primary,
                field=field,
                job_title=job_title,
                company=company,
            )
            if draft:
                new_answer = draft
                stored_canonical = stored_canonical or ""
            if new_answer:
                click.echo(f"\n[{index}/{total}] {primary[:70]}")
                click.echo(f"  → {new_answer[:120]} ({draft_source})")

        if not new_answer and (not auto_pending or prompt_on_failure):
            click.echo(f"\n{'─' * 60}")
            click.echo(f"Group {index} of {total}: {group.title}")
            click.echo(f"{'─' * 60}")
            _print_group_context(group)
            field = _infer_field_for_question(primary)
            options = field.get("options") or []
            if options:
                click.echo(f"\nLikely options: {', '.join(options)}")

            new_answer = click.prompt("\nYour answer (Enter to skip)", default="").strip()
            if not new_answer:
                click.echo("Skipped.")
                continue

        if not new_answer:
            if auto_pending:
                click.echo(f"  Skipped (no answer): {primary[:60]}")
            continue

        aliases = group.variants[1:] if len(group.variants) > 1 else None
        field = _infer_field_for_question(primary)
        if config:
            canonical, _fill = persist_answer(
                base_dir,
                primary,
                new_answer,
                field,
                config,
                company=company,
                job_title=job_title,
                canonical=stored_canonical or None,
            )
            if aliases:
                for alias in aliases:
                    save_answer(
                        base_dir,
                        alias,
                        canonical,
                        company=company,
                        job_title=job_title,
                    )
        else:
            save_answer(
                base_dir,
                primary,
                new_answer,
                company=company,
                job_title=job_title,
                aliases=aliases,
            )
        _remove_group(base_dir, group.group_id)
        answered += 1
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


def summary_for_run(base_dir: Path, *, platform: str | None = None) -> str:
    prune_answered(base_dir)
    pending_n = len(pending_question_list(base_dir))
    review_n = saved_answers_needing_review_count(base_dir)
    if not pending_n and not review_n:
        return ""
    lines: list[str] = []
    if pending_n:
        group_n = len(pending_groups(base_dir))
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
        )
        updated += 1

    click.echo(f"\n[OK] Updated {updated} answer(s).\n")
    return updated
