from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from .answers.compensation import is_numeric_ctc_question
from .answers.fields import (
    enrich_field_for_llm,
    infer_field_for_question,
    infer_field_input_type,
)
from .answers.memory_store import memory_key, sanitize_user_answer
from .answers.validation import answer_acceptable_for_field, answer_usable
from .application_questions import (
    draft_answer_for_field,
    get_saved_answer,
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
from .config import AppConfig
from .memory import load_memory
from .pending_job_ref import PendingJobRef
from .question_groups import PendingQuestionGroup, classify_question, group_pending_entries
from .utils import job_key, record_abandoned_apply

logger = logging.getLogger("job_apply")

_lock = threading.Lock()

# Messenger ask() returns these sentinels for non-answer actions.
PENDING_REPLY_SKIP = ""
PENDING_REPLY_DROP = "__drop__"
PENDING_REPLY_IGNORE = "__ignore__"


def pending_questions_path(base_dir: Path, config: AppConfig | None = None) -> Path:
    if config is not None:
        return config.pending_questions_path
    return base_dir / "data" / "pending_questions.json"


def _load(base_dir: Path, config: AppConfig | None = None) -> dict[str, Any]:
    path = pending_questions_path(base_dir, config)
    if not path.exists():
        return {"questions": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(base_dir: Path, data: dict[str, Any], config: AppConfig | None = None) -> None:
    path = pending_questions_path(base_dir, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _pending_field_meta(field: dict[str, Any] | None) -> dict[str, Any]:
    if not field:
        return {}
    keep = ("kind", "input_type", "input_mode", "options", "platform", "placeholder")
    return {k: field[k] for k in keep if field.get(k)}


def _field_for_pending_entry(label: str, entry: dict[str, Any], config: AppConfig | None) -> dict[str, Any]:
    stored = entry.get("field")
    if isinstance(stored, dict) and stored:
        field = dict(stored)
        field.setdefault("label", label)
        field.pop("input_type", None)
        return enrich_field_for_llm(field)
    return enrich_field_for_llm(infer_field_for_question(label, config))


def _memory_has_user_answer(
    base_dir: Path,
    label: str,
    field: dict[str, Any],
    config: AppConfig | None,
) -> bool:
    saved = get_saved_answer(base_dir, label, field, config=config)
    if saved and not is_chip_range_label(saved):
        if answer_usable(label, saved, field, config):
            return True
        fill = resolve_fill_answer(saved, field, config)
        if fill and answer_usable(label, fill, field, config):
            return True
    entry = load_memory(base_dir, config).get("question_answers", {}).get(memory_key(label))
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
        return answer_usable(label, ans, field, config) or bool(
            resolve_fill_answer(ans, field, config)
            and answer_usable(label, resolve_fill_answer(ans, field, config) or "", field, config)
        )
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
        or (re.search(r"\bhow (many|much)\b.*\b(years?|experience)\b", label, re.I) and not opts_lower <= {"yes", "no"})
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
        field = enrich_field_for_llm(infer_field_for_question(stored_q, config))
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
        field = _field_for_pending_entry(variant, entry if isinstance(entry, dict) else {}, config)
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
        canonical, _fill, saved = persist_answer(
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


def _saved_answer_covers_field(
    base_dir: Path,
    label: str,
    field: dict[str, Any],
    config: AppConfig | None,
) -> bool:
    """True when memory has an answer that can fill this live field (not merely stored)."""
    saved = get_saved_answer(base_dir, label, field, config=config)
    if not saved:
        return False
    if answer_usable(label, saved, field, config):
        return True
    fill = resolve_fill_answer(saved, field, config)
    return bool(fill and answer_usable(label, fill, field, config))


def queue_unanswered(
    base_dir: Path,
    *,
    source: str,
    job_title: str,
    company: str,
    job_url: str,
    labels: list[str],
    job_id: str = "",
    fields_by_label: dict[str, dict[str, Any]] | None = None,
    config: AppConfig | None = None,
) -> int:
    """Record questions we could not answer during apply. Returns newly queued count."""
    if not labels:
        return 0
    fields_by_label = fields_by_label or {}
    with _lock:
        data = _load(base_dir, config)
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
            enriched = enrich_field_for_llm({**(field or {}), "label": label})
            if _saved_answer_covers_field(base_dir, label, enriched, config):
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
                        "job_id": str(job_id or ""),
                        "at": now,
                    }
                )
                added += 1
        _save(base_dir, data, config)
    if added:
        logger.info("Queued %d unanswered question(s) for %s", added, job_title)
    return added


def pending_question_list(base_dir: Path, config: AppConfig | None = None) -> list[dict[str, Any]]:
    """Unique questions still missing answers in user_memory."""
    data = _load(base_dir, config)
    pending: list[dict[str, Any]] = []
    for entry in data.get("questions", {}).values():
        label = str(entry.get("question", "")).strip()
        if not label:
            continue
        field = _field_for_pending_entry(label, entry if isinstance(entry, dict) else {}, config)
        if _saved_answer_covers_field(base_dir, label, field, config):
            continue
        pending.append(entry)
    pending.sort(key=lambda e: len(e.get("jobs", [])), reverse=True)
    return pending


def pending_groups(base_dir: Path, config: AppConfig | None = None) -> list[PendingQuestionGroup]:
    return group_pending_entries(pending_question_list(base_dir, config))


def _group_variant_keys(group: PendingQuestionGroup) -> list[str]:
    return [question_key(variant) for variant in group.variants]


def group_notified_message_id(
    base_dir: Path,
    group: PendingQuestionGroup,
    config: AppConfig | None = None,
) -> int | None:
    """Return the Telegram message_id this group was already sent under, if any.

    Lets the messenger flow skip re-asking a question it has already sent — for
    example after a ``serve --reload`` restart — while still routing the reply,
    which quotes that original message_id.
    """
    data = _load(base_dir, config)
    questions = data.get("questions", {})
    for key in _group_variant_keys(group):
        entry = questions.get(key)
        if isinstance(entry, dict):
            mid = entry.get("notified_message_id")
            if isinstance(mid, int):
                return mid
    return None


def mark_group_notified(
    base_dir: Path,
    group: PendingQuestionGroup,
    message_id: int | None,
    config: AppConfig | None = None,
) -> None:
    """Record that this group was sent to the messenger so we never re-ask it."""
    with _lock:
        data = _load(base_dir, config)
        questions = data.get("questions", {})
        stamp = datetime.now(timezone.utc).isoformat()
        for key in _group_variant_keys(group):
            entry = questions.get(key)
            if isinstance(entry, dict):
                entry["notified_at"] = stamp
                if message_id is not None:
                    entry["notified_message_id"] = message_id
        _save(base_dir, data, config)


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


def _default_ignore_answer(
    label: str,
    field: dict[str, Any],
    config: AppConfig | None,
) -> str | None:
    """Best-effort N/A answer when the user marks a question as irrelevant."""
    field = enrich_field_for_llm({**field, "label": label})
    input_type = infer_field_input_type(label, field)
    kind = str(field.get("kind", "text"))
    options = [str(o).strip() for o in (field.get("options") or []) if str(o).strip()]

    if input_type == "ctc_numeric" or is_numeric_ctc_question(label):
        return None
    if input_type == "years_numeric" or is_skill_years_question(label):
        return "0"
    if kind == "radio" or {o.lower() for o in options} <= {"yes", "no"}:
        return next((o for o in options if o.lower() == "no"), "No")
    if options:
        for option in options:
            if re.search(r"\b(no|none|not applicable|n/?a)\b", option, re.I):
                return option
    return "Not applicable"


def _pending_job_id(job: dict[str, Any], source: str, url: str, title: str) -> str:
    """Resolve the job_id used as the apply-pipeline key for a pending job.

    Prefers the id stored when the question was queued. Legacy entries (queued
    before job_id was persisted) are reconstructed the same way each source's
    search derives it, so the abandoned key matches what the run filters on.
    """
    stored = str(job.get("job_id", "")).strip()
    if stored:
        return stored
    if source == "naukri":
        match = re.search(r"(\d{6,})", url)
        if match:
            return match.group(1)
        return hashlib.sha256(url.encode()).hexdigest()[:16]
    if source == "hirist":
        return hashlib.sha1(f"{url}|{title}".encode()).hexdigest()[:16]
    # Unknown/legacy source — fall back to the URL slug (previous behaviour).
    return url.rstrip("/").split("/")[-1] or url


def abandon_pending_jobs(
    config: AppConfig,
    jobs: list[dict[str, Any]],
    *,
    reason: str = "user dismissed",
) -> int:
    """Mark jobs abandoned so future runs skip them."""
    abandoned = 0
    for job in jobs:
        url = str(job.get("url", "")).strip()
        source = str(job.get("source", "")).strip()
        if not url or not source:
            continue
        title = str(job.get("title", ""))
        job_id = _pending_job_id(job, source, url, title)
        record_abandoned_apply(
            config.applied_jobs_path,
            job_key(source, job_id),
            {
                "source": source,
                "title": title,
                "company": str(job.get("company", "")),
                "url": url,
            },
            reason=reason,
        )
        abandoned += 1
    return abandoned


def remove_jobs_from_pending(
    base_dir: Path,
    job_urls: set[str],
    config: AppConfig | None = None,
) -> int:
    """Drop job(s) from the pending queue without answering their questions."""
    if not job_urls:
        return 0
    removed = 0
    with _lock:
        data = _load(base_dir, config)
        questions = data.get("questions", {})
        for entry in list(questions.values()):
            jobs = entry.get("jobs") or []
            kept = [j for j in jobs if str(j.get("url", "")).strip() not in job_urls]
            if len(kept) != len(jobs):
                removed += len(jobs) - len(kept)
                entry["jobs"] = kept
        for key in list(questions.keys()):
            entry = questions[key]
            if not entry.get("jobs"):
                del questions[key]
        _save(base_dir, data, config)
    return removed


def drop_pending_group_jobs(
    base_dir: Path,
    group: PendingQuestionGroup,
    config: AppConfig,
    *,
    reason: str = "user dismissed",
) -> int:
    """Abandon every job in this pending group and remove them from the queue."""
    urls = {str(job.get("url", "")).strip() for job in group.jobs if job.get("url")}
    if not urls:
        return 0
    abandoned = abandon_pending_jobs(config, group.jobs, reason=reason)
    remove_jobs_from_pending(base_dir, urls, config)
    prune_answered(base_dir, config)
    return abandoned


def ignore_pending_group(
    base_dir: Path,
    group: PendingQuestionGroup,
    config: AppConfig | None,
) -> bool:
    """Save a default N/A answer for the group and remove it from pending."""
    example = group.jobs[0] if group.jobs else {}
    primary = group.variants[0]
    company = str(example.get("company", ""))
    job_title = str(example.get("title", ""))
    pending_data = _load(base_dir, config)
    primary_entry = pending_data.get("questions", {}).get(question_key(primary), {})
    field = _field_for_pending_entry(
        primary,
        primary_entry if isinstance(primary_entry, dict) else {},
        config,
    )
    default = _default_ignore_answer(primary, field, config)
    if not default:
        return False
    stored_values = _save_pending_group_answers(
        base_dir,
        group,
        default,
        config,
        company=company,
        job_title=job_title,
    )
    if not stored_values:
        return False
    _remove_group(base_dir, group.group_id)
    return True


def parse_pending_reply(
    reply: str,
    *,
    skip_keyword: str = "skip",
    drop_keyword: str = "drop",
    ignore_keyword: str = "ignore",
) -> str:
    """Map a user reply to an answer string or a sentinel action."""
    low = reply.strip().lower()
    if low == (skip_keyword or "skip").strip().lower():
        return PENDING_REPLY_SKIP
    if low == (drop_keyword or "drop").strip().lower():
        return PENDING_REPLY_DROP
    if low == (ignore_keyword or "ignore").strip().lower():
        return PENDING_REPLY_IGNORE
    return reply.strip()


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
            has_dom_field = bool(isinstance(questions[key], dict) and questions[key].get("field"))
            if is_generic_question_label(label) or (not has_dom_field and not is_plausible_application_question(label)):
                del questions[key]
                continue
            field = _field_for_pending_entry(label, questions[key], config)
            if _saved_answer_covers_field(base_dir, label, field, config):
                del questions[key]
        _save(base_dir, data, config)
        return before - len(questions)


def _collect_retry_jobs(group: PendingQuestionGroup) -> list[PendingJobRef]:
    refs: list[PendingJobRef] = []
    for job in group.jobs:
        url = str(job.get("url", "")).strip()
        source = str(job.get("source", "")).strip()
        if not url or source not in ("naukri", "hirist"):
            continue
        refs.append(
            PendingJobRef(
                source=source,
                title=str(job.get("title", "")),
                company=str(job.get("company", "")),
                url=url,
            )
        )
    return refs


def _prompt_pending_group_action(
    *,
    draft: str | None,
    draft_source: str,
) -> tuple[str, str]:
    """Return (action, answer). action is answer|skip|drop|ignore."""
    if draft:
        click.echo(f"\nSuggested ({draft_source or 'LLM'}): {draft[:120]}" + ("…" if len(draft) > 120 else ""))
        prompt = "Action — (a)ccept suggested  (e)dit answer  (s)kip  (d)rop job(s)  (i)gnore question"
        default = "a"
    else:
        prompt = "Action — (e)nter answer  (s)kip  (d)rop job(s)  (i)gnore question"
        default = "e"

    while True:
        action = click.prompt(f"\n{prompt}", default=default).lower().strip()
        if action in ("a", "accept", "suggested") and draft:
            return "answer", draft
        if action in ("s", "skip"):
            return "skip", ""
        if action in ("d", "drop"):
            return "drop", ""
        if action in ("i", "ignore"):
            return "ignore", ""
        if action in ("e", "edit", "enter", "answer"):
            answer = click.prompt(
                "Your answer",
                default=draft or "",
                show_default=bool(draft),
            ).strip()
            if answer:
                return "answer", answer
            click.echo("Answer cannot be empty — choose skip, drop, or ignore instead.")
            continue
        click.echo("Invalid choice. Use a/e/s/d/i.")


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
        click.echo(f"\n{total} pending question group(s) deferred — run: python3 main.py answer-questions to resolve.")
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
        click.echo("  Actions: answer, (s)kip for later, (d)rop job(s), (i)gnore question forever.")
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
            pending_action, new_answer = _prompt_pending_group_action(
                draft=draft,
                draft_source=draft_source,
            )
            if pending_action == "skip":
                click.echo("Skipped for now.")
                continue
            if pending_action == "drop":
                if config:
                    dropped = drop_pending_group_jobs(base_dir, group, config)
                    click.echo(f"Dropped {dropped} job(s) — they will not be retried.")
                else:
                    urls = {str(job.get("url", "")).strip() for job in group.jobs if job.get("url")}
                    remove_jobs_from_pending(base_dir, urls, config)
                    click.echo(f"Removed {len(urls)} job(s) from pending queue.")
                continue
            if pending_action == "ignore":
                if config and ignore_pending_group(base_dir, group, config):
                    answered += 1
                    click.echo("Ignored — saved a default answer and removed from pending.")
                    jobs_to_retry.extend(_collect_retry_jobs(group))
                else:
                    click.echo("Cannot ignore this question type (e.g. salary) — use drop to skip the job instead.")
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
            click.echo("  [red]Not saved[/red] — answer failed validation. Try a fuller answer or run with --suggest.")
            continue

        _remove_group(base_dir, group.group_id)
        answered += 1
        click.echo(f"  Saved to user_memory.json ({canonical[:60]}{'…' if len(canonical) > 60 else ''}).")
        jobs_to_retry.extend(_collect_retry_jobs(group))

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


def _whatsapp_question_message(
    *,
    index: int,
    total: int,
    question: str,
    job_title: str,
    company: str,
    options: list[str],
    draft: str | None,
    skip_keyword: str,
    drop_keyword: str,
    ignore_keyword: str,
) -> str:
    lines = [f"Job question {index}/{total}"]
    where = ""
    if job_title:
        where = f"{job_title} @ {company}" if company else job_title
    if where:
        lines.append(where)
    lines.append("")
    lines.append(f"Q: {question}")
    if options:
        lines.append(f"Options: {', '.join(options[:8])}")
    if draft:
        snippet = draft if len(draft) <= 160 else draft[:160] + "…"
        lines.append(f"Suggested: {snippet}")
    lines.append("")
    lines.append(
        f'Reply with your answer, or "{skip_keyword}" (later), '
        f'"{drop_keyword}" (skip job), "{ignore_keyword}" (not applicable).'
    )
    return "\n".join(lines)


def _prepare_group_question(
    base_dir: Path,
    config: AppConfig,
    group: PendingQuestionGroup,
    index: int,
    total: int,
    *,
    use_llm_draft: bool,
    skip_keyword: str,
    drop_keyword: str,
    ignore_keyword: str,
) -> dict[str, Any]:
    """Build the message + context for one pending group (shared by both flows)."""
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

    draft = None
    if use_llm_draft:
        try:
            draft_result = draft_answer_for_field(
                config,
                question=primary,
                field=field,
                job_title=job_title,
                company=company,
            )
            draft = draft_result.fill
        except Exception as exc:
            logger.debug("Messenger draft failed for %s: %s", primary[:60], exc)

    options = list(field.get("options") or [])
    message = _whatsapp_question_message(
        index=index,
        total=total,
        question=primary,
        job_title=job_title,
        company=company,
        options=options,
        draft=draft,
        skip_keyword=skip_keyword,
        drop_keyword=drop_keyword,
        ignore_keyword=ignore_keyword,
    )
    return {"primary": primary, "company": company, "job_title": job_title, "message": message}


def _reply_send_kwargs(client, reply_to_message_id: int | None) -> dict[str, Any]:
    """Quote the user's reply in confirmations when the transport supports it."""
    if reply_to_message_id is not None and getattr(client, "supports_reply_routing", False):
        return {"reply_to_message_id": reply_to_message_id}
    return {}


async def _process_messenger_reply(
    base_dir: Path,
    config: AppConfig,
    client,
    group: PendingQuestionGroup,
    *,
    primary: str,
    company: str,
    job_title: str,
    reply: str,
    reply_to_message_id: int | None = None,
) -> tuple[str, list[PendingJobRef]]:
    """Apply one already-parsed reply to a group.

    Returns ``(outcome, jobs_to_retry)`` where outcome is one of:
    ``"answered"`` (saved/ignored), ``"skipped"``, ``"dropped"``, or ``"retry"``
    (could not save — caller should keep the question open for another reply).
    """
    send_kw = _reply_send_kwargs(client, reply_to_message_id)
    if reply == PENDING_REPLY_SKIP:
        return "skipped", []
    if reply == PENDING_REPLY_DROP:
        dropped = drop_pending_group_jobs(base_dir, group, config)
        with contextlib.suppress(Exception):
            await client.send(f"Dropped {dropped} job(s) — will not retry.", **send_kw)
        return "dropped", []
    if reply == PENDING_REPLY_IGNORE:
        if ignore_pending_group(base_dir, group, config):
            with contextlib.suppress(Exception):
                await client.send("Ignored — saved default answer.", **send_kw)
            return "answered", _collect_retry_jobs(group)
        with contextlib.suppress(Exception):
            await client.send("Cannot ignore this question type — use drop to skip the job.", **send_kw)
        return "retry", []

    stored_values = _save_pending_group_answers(
        base_dir,
        group,
        reply,
        config,
        company=company,
        job_title=job_title,
    )
    if not stored_values:
        with contextlib.suppress(Exception):
            await client.send("Could not save that answer (failed validation). Try again.", **send_kw)
        return "retry", []

    _remove_group(base_dir, group.group_id)
    with contextlib.suppress(Exception):
        await client.send(f"Saved: {stored_values[0][:80]}", **send_kw)
    return "answered", _collect_retry_jobs(group)


async def answer_pending_groups_via_messenger(
    base_dir: Path,
    config: AppConfig,
    client,
    *,
    applied_count: int | None = None,
    send_heads_up: bool = True,
    per_question_timeout: int | None = None,
) -> tuple[int, list[PendingJobRef]]:
    """Ask each pending question over a messenger and save the routed replies.

    Reply routing is required: ``client`` must advertise ``supports_reply_routing``
    and expose ``wait_for_reply_routed`` (Telegram). All questions are sent up
    front and each reply is matched to the question its ``reply_to`` quotes, so
    you can answer them in any order. A message that isn't a reply to a tracked
    question is ignored — we never guess by send order. Transports without reply
    routing are skipped. ``per_question_timeout`` overrides the per-reply wait
    (the listener uses a very large value to wait indefinitely).
    """
    prune_answered(base_dir, config)
    groups = pending_groups(base_dir, config)
    if not groups:
        return 0, []

    routed = bool(getattr(client, "supports_reply_routing", False)) and hasattr(client, "wait_for_reply_routed")
    if not routed:
        logger.warning(
            "Messenger transport %s has no reply routing — pending questions can only "
            "be answered by replying to a specific question. Skipping.",
            type(client).__name__,
        )
        return 0, []

    total = len(groups)
    use_llm_draft = bool(config.llm.enabled)
    skip_keyword = getattr(client, "skip_keyword", "skip")
    drop_keyword = getattr(client, "drop_keyword", "drop")
    ignore_keyword = getattr(client, "ignore_keyword", "ignore")

    new_groups = sum(1 for g in groups if group_notified_message_id(base_dir, g, config) is None)
    if send_heads_up and new_groups:
        applied_part = f"{applied_count} applied" if applied_count is not None else "Run complete"
        question_word = "question" if new_groups == 1 else "questions"
        try:
            await client.send(
                f"✅ {applied_part} — {new_groups} {question_word} need your input.\n"
                "Reply to each question (tap it → Reply) and I'll match your answer to it. "
                f'Reply with your answer, or "{skip_keyword}" (later), '
                f'"{drop_keyword}" (skip job), "{ignore_keyword}" (not applicable).'
            )
        except Exception as exc:
            logger.debug("Messenger heads-up message failed: %s", exc)

    return await _answer_pending_groups_reply_routed(
        base_dir,
        config,
        client,
        groups,
        total=total,
        use_llm_draft=use_llm_draft,
        skip_keyword=skip_keyword,
        drop_keyword=drop_keyword,
        ignore_keyword=ignore_keyword,
        per_question_timeout=per_question_timeout,
    )


async def _answer_pending_groups_reply_routed(
    base_dir: Path,
    config: AppConfig,
    client,
    groups: list[PendingQuestionGroup],
    *,
    total: int,
    use_llm_draft: bool,
    skip_keyword: str,
    drop_keyword: str,
    ignore_keyword: str,
    per_question_timeout: int | None,
) -> tuple[int, list[PendingJobRef]]:
    """Send all questions, then route each reply to the question it quotes.

    Telegram tags a reply with the ``message_id`` it was sent in answer to, so we
    can save out-of-order replies against the right question. Replies that don't
    quote a specific question (a plain message) are ignored — we never guess by
    sequence, so the user must explicitly reply to the question they're answering.
    """
    states_by_msg_id: dict[int, dict[str, Any]] = {}
    order: list[dict[str, Any]] = []

    for index, group in enumerate(groups, 1):
        # Already asked before (e.g. an earlier run or a `serve --reload`
        # restart)? Don't re-send — just reuse the original message_id so the
        # reply that quotes it still routes to this question.
        message_id = group_notified_message_id(base_dir, group, config)
        example = group.jobs[0] if group.jobs else {}
        primary = group.variants[0]
        company = str(example.get("company", ""))
        job_title = str(example.get("title", ""))
        if message_id is None:
            prep = _prepare_group_question(
                base_dir,
                config,
                group,
                index,
                total,
                use_llm_draft=use_llm_draft,
                skip_keyword=skip_keyword,
                drop_keyword=drop_keyword,
                ignore_keyword=ignore_keyword,
            )
            primary = prep["primary"]
            company = prep["company"]
            job_title = prep["job_title"]
            try:
                message_id = await client.send(prep["message"])
            except Exception as exc:
                logger.warning("Messenger send failed for %s: %s", primary[:60], exc)
                break
            mark_group_notified(base_dir, group, message_id, config)
            await asyncio.sleep(0.3)  # gentle pacing so we don't trip Telegram rate limits
        state = {
            "group": group,
            "primary": primary,
            "company": company,
            "job_title": job_title,
            "message_id": message_id,
            "done": False,
        }
        order.append(state)
        if message_id is not None:
            states_by_msg_id[message_id] = state

    answered = 0
    jobs_to_retry: list[PendingJobRef] = []
    remaining = sum(1 for s in order if not s["done"])

    while remaining > 0:
        upd = await client.wait_for_reply_routed(timeout=per_question_timeout)
        if upd is None:
            logger.info("No messenger reply within timeout — leaving remaining questions pending.")
            with contextlib.suppress(Exception):
                await client.send(
                    "No reply received — remaining questions saved for later. "
                    "Run again or use: python main.py answer-questions"
                )
            break

        reply_to = upd.get("reply_to")
        state = states_by_msg_id.get(reply_to) if reply_to is not None else None
        if state is None:
            # Not a reply to a tracked question (a plain message, or a reply to
            # something we don't track). It isn't linked to any question, so we
            # never guess by order — ignore it.
            logger.debug("Ignoring messenger message not routed to a question.")
            continue
        if state["done"]:
            with contextlib.suppress(Exception):
                await client.send(
                    "That one's already answered — reply to another question instead.",
                    **_reply_send_kwargs(client, reply_to),
                )
            continue

        parsed = parse_pending_reply(
            upd.get("text") or "",
            skip_keyword=skip_keyword,
            drop_keyword=drop_keyword,
            ignore_keyword=ignore_keyword,
        )
        outcome, retry = await _process_messenger_reply(
            base_dir,
            config,
            client,
            state["group"],
            primary=state["primary"],
            company=state["company"],
            job_title=state["job_title"],
            reply=parsed,
            reply_to_message_id=state["message_id"],
        )
        if outcome == "retry":
            continue  # keep the question open so the user can correct their answer
        state["done"] = True
        remaining -= 1
        if outcome == "answered":
            answered += 1
            jobs_to_retry.extend(retry)

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


def summary_for_run(base_dir: Path, *, platform: str | None = None, config: AppConfig | None = None) -> str:
    prune_answered(base_dir, config)
    pending_n = len(pending_question_list(base_dir, config))
    review_n = saved_answers_needing_review_count(base_dir)
    if not pending_n and not review_n:
        return ""
    lines: list[str] = []
    if pending_n:
        group_n = len(pending_groups(base_dir, config))
        lines.append(f"[yellow]{pending_n} question(s) in {group_n} group(s) need answers.[/yellow]")
    if review_n:
        lines.append(f"[yellow]{review_n} saved answer(s) need your review.[/yellow]")
        lines.append("Run: [bold]python3 main.py answer-questions --review[/bold]")
    rerun = f"python3 main.py run --platform {platform}" if platform and platform != "all" else "python3 main.py run"
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
        entries = [e for e in entries if needs_review_answer(e["question"], e["answer"])]
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
