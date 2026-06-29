"""Canonical vs fill formatting — always run before persisting or filling a form."""

from __future__ import annotations

from typing import Any

from ..config import AppConfig
from .fields import enrich_field_for_llm
from .memory_store import canonicalize_stored_answer, resolve_fill_answer
from .validation import answer_acceptable_for_field


def finalize_answer_for_field(
    question: str,
    field: dict[str, Any],
    config: AppConfig | None,
    *,
    raw_answer: str,
    canonical: str | None = None,
) -> tuple[str, str] | None:
    """
    Map raw/semantic answer → canonical (store) + fill (form).
    Returns None when the value cannot be shaped for this field.
    """
    text = (raw_answer or "").strip()
    if not text:
        return None

    field = enrich_field_for_llm(field)
    # Open free-text fields have no canonical/fill distinction (the canonical slot
    # exists to map a stored value onto choice/chip option labels). When the model
    # returns a canonical that differs from its actual answer — e.g. answer "Yes",
    # canonical "Bangalore" for "Are you based out of Bangalore? If yes, which
    # locality?" — using the canonical would silently replace the real answer. On
    # plain text fields, trust the answer the model chose.
    from .fields import is_free_text_field

    if canonical and is_free_text_field(field):
        canonical = None
    stored = (canonical or "").strip() or canonicalize_stored_answer(question, text, field, config)
    if not stored:
        return None

    fill = resolve_fill_answer(stored, field, config)
    if not fill or not answer_acceptable_for_field(question, fill, field):
        return None
    return fill, stored
