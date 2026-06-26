from __future__ import annotations

from typing import Any

import click

from .chips import _normalize_to_option


def prompt_confirm_new_answer(
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
                    show_default=True,
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


_prompt_confirm_new_answer = prompt_confirm_new_answer
