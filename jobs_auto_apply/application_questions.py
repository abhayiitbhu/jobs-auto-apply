from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

import click
from playwright.async_api import Page

from .config import AppConfig
from .memory import load_memory, save_memory
from .page_load import ensure_page_ready

logger = logging.getLogger("job_apply")

COVER_NOTE_HINT = re.compile(r"note|message|cover", re.I)

GENERIC_QUESTION_LABELS = frozenset(
    {
        "enter your answer",
        "type here",
        "your answer",
        "answer",
    }
)

_interactive_prompt_lock_instance: asyncio.Lock | None = None


def _interactive_prompt_lock() -> asyncio.Lock:
    global _interactive_prompt_lock_instance
    if _interactive_prompt_lock_instance is None:
        _interactive_prompt_lock_instance = asyncio.Lock()
    return _interactive_prompt_lock_instance


def _question_key(text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def question_key(text: str) -> str:
    return _question_key(text)


def is_generic_question_label(label: str) -> bool:
    norm = re.sub(r"\s+", " ", label.strip().lower())
    return not norm or norm in GENERIC_QUESTION_LABELS or len(norm) < 3


_JOB_LISTING_NOISE = re.compile(
    r"posted\s+(today|yesterday|\d+\s+days?\s+ago)"
    r"|\bpremium\b"
    r"|\binfosave\b"
    r"|similar jobs"
    r"|\d+\s*-\s*\d+\s*yrs",
    re.I,
)

_NON_QUESTION_NOISE = re.compile(
    r"thank you for your|thanks for (your )?response|we have received your|"
    r"application submitted|successfully applied|your application has been",
    re.I,
)

_YEAR_RANGE_LABEL = re.compile(r"^\d+\s*[-–]\s*\d+\+?$")

_QUESTION_HINT = re.compile(
    r"\?|^(do |are |have |what |when |where |how |which |please |enter |select |mention |specify )",
    re.I,
)

_CONSENT_HINT = re.compile(
    r"\b(agree|consent|terms|conditions|confirm|acknowledge|accept|declare)\b",
    re.I,
)

_FIELD_TOPIC = re.compile(
    r"\b(notice|experience|ctc|salary|location|employer|relocation|pan\b|uan\b|join|available|linkedin|portfolio|phone|email|skill|years?|name|education|employer|hometown)\b",
    re.I,
)

_PROFILE_FIELD_LABEL = re.compile(
    r"\b(middle name|first name|last name|full name|father'?s? name|mother'?s? name|"
    r"highest level of education|education obtained|date of birth|gender|marital status|"
    r"hometown|ectc|expected ctc|current ctc|previously employed)\b",
    re.I,
)

_OPTIONAL_NAME_FIELD = re.compile(
    r"\b(middle name|first name|last name|maiden name|nick\s*name|nickname)\b",
    re.I,
)


def is_plausible_application_question(label: str) -> bool:
    """Reject scraped job-card chrome, answer options, and other non-question labels."""
    text = re.sub(r"\s+", " ", label.strip())
    if is_generic_question_label(text):
        return False
    if _JOB_LISTING_NOISE.search(text):
        return False
    if _NON_QUESTION_NOISE.search(text):
        return False
    if _YEAR_RANGE_LABEL.match(text):
        return False
    if re.match(r"^\d+\+$", text):
        return False
    if re.search(r"\b(save|share|premium|info)\b", text, re.I) and "?" not in text:
        return False
    if len(text) > 200:
        return False
    if _QUESTION_HINT.search(text):
        return True
    if _CONSENT_HINT.search(text) and len(text) < 120:
        return True
    if _FIELD_TOPIC.search(text) and len(text) < 100:
        return True
    if _PROFILE_FIELD_LABEL.search(text) and len(text) < 120:
        return True
    return False


def is_placeholder_answer(answer: str) -> bool:
    """Detect answers that are tips/placeholders rather than real responses."""
    a = answer.strip().lower()
    if not a:
        return True
    return "e.g." in a or a.startswith("tip:") or "— e.g." in a


def needs_review_answer(question: str, answer: str) -> bool:
    """Saved answer looks wrong (e.g. RAG dumped resume text into a short field)."""
    if is_placeholder_answer(answer):
        return True
    q = question.lower()
    a = answer.strip()
    if len(a) > 150 and any(
        token in q
        for token in (
            "how many",
            "years",
            "ctc",
            "salary",
            "usd",
            "monthly",
            "url",
            "linkedin",
            "when would",
            "available to start",
            "rate your",
            "comfortable",
            "based in india",
            "cet working",
            "yes",
            "no",
        )
    ):
        return True
    if "linkedin" in q and "linkedin.com" not in a.lower():
        return True
    if a.lower().startswith("results-driven"):
        return True
    if re.search(r"\boffers?\b", q) and len(a) > 20:
        return True
    if is_employer_check_question(q) and (
        len(a) > 40 or not re.match(r"^(yes|no)\b", a, re.I)
    ):
        return True
    return False


from .answer_suggest import is_employer_check_question


def _value_in_answer_range(value: int, option: str) -> bool:
    opt = option.lower()
    lt_m = re.search(r"<\s*(\d+)", opt)
    if lt_m:
        return value < int(lt_m.group(1))
    range_m = re.search(r"(\d+)\s*[-–]\s*(\d+)", opt)
    if range_m:
        return int(range_m.group(1)) <= value <= int(range_m.group(2))
    plus_m = re.search(r"(\d+)\s*\+", opt)
    if plus_m:
        return value >= int(plus_m.group(1))
    single = re.search(r"(\d+)", opt)
    if single:
        return value == int(single.group(1))
    return False


_YEAR_CHIP_LABEL = re.compile(r"<\s*\d+|\d+\s*[-–]\s*\d+|\d+\s*\+", re.I)


def is_chip_range_label(answer: str) -> bool:
    """True when answer is a UI chip label (e.g. '<6 years') rather than a canonical value."""
    return bool(_YEAR_CHIP_LABEL.search(answer.strip()))


_YEAR_EXPERIENCE_Q = re.compile(
    r"\b(how many|years?)\b.*\b(experience|hands?\s*on)\b|\bexperience\b.*\byears?\b",
    re.I,
)


def _infer_field_for_question(question: str) -> dict[str, Any]:
    """Best-effort field metadata when options aren't known (e.g. pending queue)."""
    label = question.strip()
    q = label.lower()
    if re.search(r"\bhow many\b.*\byears?\b|\byears?\b.*\bexperience\b", q):
        return {
            "kind": "radio",
            "label": label,
            "options": ["No experience", "<6 years", "6-8 years", "8+ years"],
        }
    if re.search(
        r"\b(do you have|have you|are you|do you|any offers?|holding any offer|"
        r"received an offer|currently have)\b",
        q,
    ):
        return {"kind": "radio", "label": label, "options": ["Yes", "No"]}
    if re.search(r"\b(date of birth|dob|birth date)\b", q):
        return {"kind": "text", "label": label, "input_type": "date"}
    if re.search(r"\b(yes|no|available|willing|employed|associated|immediate|offer)\b", q):
        return {"kind": "radio", "label": label, "options": ["Yes", "No"]}
    return {"kind": "text", "label": label}


def answer_acceptable_for_field(
    question: str,
    answer: str,
    field: dict[str, Any],
) -> bool:
    """Reject resume dumps, placeholders, and answers that don't fit field options."""
    if not answer or is_placeholder_answer(answer):
        return False
    if needs_review_answer(question, answer):
        return False
    if not _saved_answer_fits_field(answer, field):
        return False
    return True


def draft_answer_for_field(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    jd: str = "",
    job_title: str = "",
    company: str = "",
) -> tuple[str | None, str | None, str]:
    """
    RAG → validate → LLM (confidence threshold) → validate.
    Returns (fill_answer, canonical, source) or (None, None, "").
    """
    draft: str | None = None
    draft_source = ""
    draft_canonical: str | None = None

    if config.application.rag_answer_questions:
        from .rag_answers import generate_rag_answer

        rag = generate_rag_answer(
            config,
            question=question,
            field=field,
            jd=jd,
            job_title=job_title,
        )
        if rag and answer_acceptable_for_field(question, rag, field):
            return rag, None, "RAG"
        if rag:
            logger.info(
                "Rejected RAG draft for %s field: %s",
                field.get("kind", "text"),
                question[:60],
            )

    if config.llm.enabled:
        from .llm_answers import generate_llm_decision, retrieve_similar_answers

        similar = (
            retrieve_similar_answers(config, question)
            if config.application.rag_answer_questions
            else None
        )
        decision = generate_llm_decision(
            config,
            question=question,
            field=field,
            jd=jd,
            job_title=job_title,
            company=company,
            similar_answers=similar,
        )
        if decision:
            if decision.confidence >= config.llm.min_confidence:
                if answer_acceptable_for_field(question, decision.answer, field):
                    return decision.answer, decision.canonical, "LLM"
                logger.info(
                    "Rejected LLM draft for %s field: %s",
                    field.get("kind", "text"),
                    question[:60],
                )
            else:
                logger.info(
                    "LLM confidence below threshold (%.2f < %.2f): %s",
                    decision.confidence,
                    config.llm.min_confidence,
                    question[:60],
                )

    return None, None, draft_source


def canonicalize_stored_answer(
    question: str,
    answer: str,
    field: dict[str, Any] | None = None,
    config: AppConfig | None = None,
) -> str:
    """Store specific values (e.g. '4') instead of chip labels (e.g. '<6 years')."""
    text = answer.strip()
    if not text:
        return text

    label = question.lower()
    options = [str(o).strip() for o in (field or {}).get("options", []) if str(o).strip()]

    plain_num = re.fullmatch(r"(\d+)(?:\s*years?)?", text, re.I)
    if plain_num:
        return plain_num.group(1)

    if _YEAR_EXPERIENCE_Q.search(label) or (
        options and any(re.search(r"<\s*\d+|\d+\s*[-–]\s*\d+|\d+\s*\+", o) for o in options)
    ):
        num = re.search(r"(\d+)", text)
        if num and not re.search(r"<\s*\d+|\d+\s*[-–]\s*\d+", text):
            return num.group(1)
        if config and config.profile.years_experience:
            for opt in options:
                if _value_in_answer_range(config.profile.years_experience, opt):
                    return str(config.profile.years_experience)
            return str(config.profile.years_experience)

    if options and {o.lower() for o in options} <= {"yes", "no"}:
        if re.search(r"\byes\b", text, re.I):
            return "Yes"
        if re.search(r"\bno\b", text, re.I):
            return "No"

    return text


def resolve_fill_answer(stored: str, field: dict[str, Any]) -> str:
    """Map canonical stored value to the value needed to fill the form."""
    text = stored.strip()
    if not text:
        return text

    kind = str(field.get("kind", "text"))
    options = [str(o).strip() for o in field.get("options", []) if str(o).strip()]
    label = str(field.get("label", ""))

    if kind in ("radio", "checkbox_group") and options:
        picked = _normalize_to_option(text, options)
        if picked:
            return picked
        num = re.search(r"(\d+)", text)
        if num:
            value = int(num.group(1))
            for opt in options:
                if _value_in_answer_range(value, opt):
                    return opt
        if re.search(r"\b(yes|no)\b", text, re.I) and {o.lower() for o in options} <= {"yes", "no"}:
            return "Yes" if re.search(r"\byes\b", text, re.I) else "No"

    if kind == "radio" and not options and re.search(r"\b(yes|no)\b", text, re.I):
        return "Yes" if re.search(r"\byes\b", text, re.I) else "No"

    # Text fields: append "years" when question asks for years and answer is numeric only
    if kind in ("input", "text", "textarea") and re.search(r"\byears?\b", label, re.I):
        if re.fullmatch(r"\d+", text):
            return f"{text} years"

    return text


def persist_answer(
    base_dir,
    question: str,
    fill_answer: str,
    field: dict[str, Any],
    config: AppConfig,
    *,
    company: str = "",
    job_title: str = "",
    canonical: str | None = None,
) -> tuple[str, str]:
    """Save canonical answer to memory; return (canonical, fill_value) for the form."""
    stored = canonical or canonicalize_stored_answer(question, fill_answer, field, config)
    save_answer(
        base_dir,
        question,
        stored,
        company=company,
        job_title=job_title,
    )
    return stored, resolve_fill_answer(stored, field)


def _saved_answer_fits_field(answer: str, field: dict[str, Any]) -> bool:
    """Reject cross-group reuse when a text answer cannot fill a Yes/No control."""
    label = str(field.get("label", ""))
    if _OPTIONAL_NAME_FIELD.search(label):
        if re.fullmatch(r"\d+", answer.strip()):
            return False
    kind = str(field.get("kind", "text"))
    if kind in ("radio", "checkbox") and len(answer.strip()) > 80:
        return False
    if kind not in ("radio", "checkbox"):
        return True
    options = [str(o).strip() for o in field.get("options", []) if str(o).strip()]
    a = answer.strip().lower()
    if kind == "radio" and options:
        opts = [o.lower() for o in options]
        if set(opts).issubset({"yes", "no"}) and len(opts) >= 2:
            return a in ("yes", "no")
        if any(a == o or a in o or o in a for o in opts):
            return True
        num = re.search(r"(\d+)", a)
        if num:
            value = int(num.group(1))
            if any(_value_in_answer_range(value, o) for o in options):
                return True
        if any(w in a for w in ("immediate", "lwd", "serving")) and any(
            re.search(r"immediate|serving", o, re.I) for o in options
        ):
            return True
        return False
    if kind == "checkbox":
        return a in ("yes", "no", "true", "false", "1", "0", "checked", "agree", "accept")
    if kind == "checkbox_group" and options:
        label = str(field.get("label", ""))
        if re.search(
            r"\bselect\b.{0,30}\b(city|cities)\b|\b(city|cities)\b.{0,30}\b(residing|relocate)\b",
            label,
            re.I,
        ):
            if a in ("yes", "y"):
                return True
            for part in re.split(r"[,;|]", answer):
                token = part.strip().lower().split(",")[0].strip()
                if len(token) < 3:
                    continue
                if any(token in o.lower() or o.lower().startswith(token) for o in options):
                    return True
            return False
        for opt in options:
            ol = opt.lower()
            if a == ol or a in ol or ol in a:
                return True
        return False
    return True


def _normalize_to_option(answer: str, options: list[str]) -> str | None:
    a = answer.strip().lower()
    if not a:
        return None
    for opt in options:
        o = opt.strip()
        ol = o.lower()
        if ol == a or a in ol or ol in a:
            return o
    return None


def _prompt_confirm_new_answer(
    label: str,
    field: dict[str, Any],
    draft: str | None,
    *,
    job_title: str = "",
    company: str = "",
) -> str | None:
    """Ask user to accept, edit, or skip a new question before applying."""
    kind = str(field.get("kind", "text"))
    options = [str(o).strip() for o in field.get("options", []) if str(o).strip()]
    draft = (draft or "").strip()
    if draft and kind == "radio" and options:
        normalized = _normalize_to_option(draft, options)
        if normalized:
            draft = normalized

    click.echo(f"\n{'─' * 60}")
    click.echo("New question — confirm before applying")
    click.echo(f"{'─' * 60}")
    click.echo(label)
    if job_title:
        where = f"{job_title} @ {company}" if company else job_title
        click.echo(f"\nJob: {where}")
    if kind == "radio" and options:
        click.echo(f"\nType: radio — options: {', '.join(options)}")
    elif kind == "checkbox":
        click.echo("\nType: checkbox (Yes / No)")
    elif kind == "checkbox_group" and options:
        click.echo(f"\nType: multi-select — options: {', '.join(options)}")
    if draft:
        click.echo(f"\nSuggested answer: {draft}")

    while True:
        if draft:
            prompt = "Action — (a)ccept  (e)dit  (s)kip"
            default = "a"
        else:
            prompt = "Action — (e)nter answer  (s)kip"
            default = "e"

        action = click.prompt(f"\n{prompt}", default=default).lower().strip()

        if action in ("s", "skip"):
            click.echo("Skipped — this job will not be submitted until answered.")
            return None

        if action in ("a", "accept", "") and draft:
            click.echo("Confirmed.")
            return draft

        if action in ("e", "edit", "a", "accept", ""):
            if kind == "radio" and options:
                hint = f" ({'/'.join(options)})" if len(options) <= 6 else ""
                raw = click.prompt(
                    f"Your answer{hint}",
                    default=draft or options[0],
                    show_default=bool(draft or options),
                ).strip()
                picked = _normalize_to_option(raw, options) if raw else None
                if picked:
                    click.echo("Confirmed.")
                    return picked
                click.echo(f"Pick one of: {', '.join(options)}")
                continue
            if kind == "checkbox":
                raw = click.prompt(
                    "Your answer (Yes/No)",
                    default=draft or "Yes",
                    show_default=bool(draft or True),
                ).strip()
                if raw.lower() in ("yes", "no", "y", "n"):
                    click.echo("Confirmed.")
                    return "Yes" if raw.lower() in ("yes", "y") else "No"
                click.echo("Enter Yes or No.")
                continue
            raw = click.prompt(
                "Your answer",
                default=draft,
                show_default=bool(draft),
            ).strip()
            if raw:
                click.echo("Confirmed.")
                return raw
            click.echo("Answer cannot be empty.")
            continue

        click.echo("Invalid choice. Use a, e, or s.")


def get_saved_answer(
    base_dir,
    question: str,
    field: dict[str, Any] | None = None,
) -> str | None:
    from .question_groups import classify_question

    key = _question_key(question)
    answers = load_memory(base_dir).get("question_answers", {})
    entry = answers.get(key)
    if isinstance(entry, dict):
        ans = str(entry.get("answer", "")) or None
        if ans and _answer_usable(question, ans, field):
            return ans

    group_id = classify_question(question)
    for entry in answers.values():
        if not isinstance(entry, dict):
            continue
        stored_q = str(entry.get("question", ""))
        if stored_q and classify_question(stored_q) == group_id:
            ans = str(entry.get("answer", "")) or None
            if ans and _answer_usable(question, ans, field):
                return ans
    return None


def _answer_usable(question: str, answer: str, field: dict[str, Any] | None) -> bool:
    if needs_review_answer(question, answer):
        return False
    if field and not _saved_answer_fits_field(answer, field):
        return False
    return True


def save_answer(
    base_dir,
    question: str,
    answer: str,
    *,
    company: str = "",
    job_title: str = "",
    aliases: list[str] | None = None,
) -> None:
    labels = [question.strip()]
    if aliases:
        for alias in aliases:
            alias = alias.strip()
            if alias and alias not in labels:
                labels.append(alias)
    data = load_memory(base_dir)
    answers = data.setdefault("question_answers", {})
    for label in labels:
        key = _question_key(label)
        answers[key] = {
            "question": label,
            "answer": answer.strip(),
            "company": company,
            "job_title": job_title,
        }
    save_memory(base_dir, data)


async def discover_questions(page: Page) -> list[dict[str, Any]]:
    """Find mandatory application fields beyond the cover-note textarea."""
    await ensure_page_ready(page, for_form=True)
    fields: list[dict[str, Any]] = []
    container = page.locator('[role="dialog"]').last
    if await container.count() == 0:
        container = page.locator("body")

    textareas = container.locator("textarea:visible")
    for i in range(await textareas.count()):
        el = textareas.nth(i)
        label = await _label_for(page, el)
        placeholder = (await el.get_attribute("placeholder")) or ""
        if COVER_NOTE_HINT.search(label + placeholder):
            continue
        resolved = label or placeholder
        if is_generic_question_label(resolved):
            continue
        fields.append({"kind": "textarea", "label": resolved, "index": i})

    inputs = container.locator('input[type="text"]:visible, input:not([type]):visible')
    for i in range(await inputs.count()):
        el = inputs.nth(i)
        label = await _label_for(page, el)
        placeholder = (await el.get_attribute("placeholder")) or ""
        if not label and not placeholder:
            continue
        resolved = label or placeholder
        if is_generic_question_label(resolved):
            continue
        fields.append({"kind": "input", "label": resolved, "index": i})

    selects = container.locator("select:visible")
    for i in range(await selects.count()):
        el = selects.nth(i)
        label = await _label_for(page, el)
        fields.append({"kind": "select", "label": label or f"Question {i+1}", "index": i})

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for f in fields:
        lab = f["label"].strip()
        if not lab or lab in seen:
            continue
        seen.add(lab)
        unique.append(f)
    return unique


async def _label_for(page: Page, el) -> str:
    el_id = await el.get_attribute("id")
    if el_id:
        label = page.locator(f'label[for="{el_id}"]')
        if await label.count() > 0:
            return (await label.first.inner_text()).strip()
    aria = await el.get_attribute("aria-label")
    return (aria or "").strip()


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
    use_interactive = (
        config.application.interactive_questions if interactive is None else interactive
    )
    use_confirm_new = (
        config.application.confirm_new_answers if confirm_new is None else confirm_new
    )
    job_title = getattr(job, "title", "") or ""
    company = getattr(job, "company", "") or ""

    for field in questions:
        label = field["label"]
        saved = get_saved_answer(config.base_dir, label, field)
        if saved and is_chip_range_label(saved):
            saved = canonicalize_stored_answer(label, saved, field, config)
            save_answer(
                config.base_dir,
                label,
                saved,
                company=company,
                job_title=job_title,
            )
        if (
            saved
            and not is_placeholder_answer(saved)
            and answer_acceptable_for_field(label, saved, field)
        ):
            answers[label] = resolve_fill_answer(saved, field)
            logger.info("Using saved answer for: %s", label[:60])
            continue
        if saved and not is_placeholder_answer(saved):
            logger.info(
                "Saved answer not valid for %s field; regenerating: %s",
                field.get("kind", "text"),
                label[:60],
            )

        draft, draft_canonical, draft_source = draft_answer_for_field(
            config,
            question=label,
            field=field,
            jd=jd,
            job_title=job_title,
            company=company,
        )

        if defer_new:
            if draft:
                _stored, fill_value = persist_answer(
                    config.base_dir,
                    label,
                    draft,
                    field,
                    config,
                    company=company,
                    job_title=job_title,
                    canonical=draft_canonical,
                )
                answers[label] = fill_value
                logger.info("%s live answer (confidence ok): %s", draft_source, label[:60])
            else:
                logger.info("Deferred question (will queue): %s", label[:60])
            continue

        if use_confirm_new:
            async with _interactive_prompt_lock():
                confirmed = _prompt_confirm_new_answer(
                    label,
                    field,
                    draft,
                    job_title=job_title,
                    company=company,
                )
            if not confirmed:
                logger.info("Skipped new question: %s", label[:60])
                continue
            answers[label] = persist_answer(
                config.base_dir,
                label,
                confirmed,
                field,
                config,
                company=company,
                job_title=job_title,
            )[1]
            logger.info("Confirmed answer for: %s", label[:60])
            continue

        if draft:
            answers[label] = persist_answer(
                config.base_dir,
                label,
                draft,
                field,
                config,
                company=company,
                job_title=job_title,
                canonical=draft_canonical,
            )[1]
            logger.info("%s answer for: %s", draft_source or "Auto", label[:60])
            continue

        if not use_interactive:
            logger.info("No answer for: %s", label[:60])
            continue

        async with _interactive_prompt_lock():
            click.echo(f"\nApplication question: {label}")
            entered = click.prompt("Your answer", default="").strip()
        if not entered:
            continue

        answers[label] = persist_answer(
            config.base_dir,
            label,
            entered,
            field,
            config,
            company=company,
            job_title=job_title,
        )[1]
    return answers


async def fill_questions(page: Page, answers: dict[str, str]) -> None:
    await ensure_page_ready(page, for_form=True)
    container = page.locator('[role="dialog"]').last
    if await container.count() == 0:
        container = page.locator("body")

    for question, answer in answers.items():
        if not answer:
            continue
        label = container.locator("label").filter(has_text=re.compile(re.escape(question[:40]), re.I))
        if await label.count() > 0:
            for_id = await label.first.get_attribute("for")
            if for_id:
                target = container.locator(f"#{for_id}")
                if await target.count() > 0:
                    await target.fill(answer)
                    continue
        field = container.locator(
            f'textarea[placeholder*="{question[:20]}" i], input[placeholder*="{question[:20]}" i]'
        )
        if await field.count() > 0:
            await field.first.fill(answer)
