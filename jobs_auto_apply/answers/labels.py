"""Question label normalization and noise filtering."""

from __future__ import annotations

import asyncio
import re

COVER_NOTE_HINT = re.compile(r"note|message|cover", re.I)

GENERIC_QUESTION_LABELS = frozenset(
    {
        "enter your answer",
        "type here",
        "your answer",
        "answer",
    }
)

_INVALID_QUESTION_PREFIX = re.compile(r"^the input seems invalid\.?\s*", re.I)

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
    r"\?|^(do |are |have |what |when |where |how |which |please |enter |select |mention |specify )"
    # Mid-sentence interrogatives: many recruiter questions are phrased as a
    # statement + "…will you be able to…" with no leading keyword or "?".
    r"|\b(will|would|can|could|are|do|did|have|should)\s+(?:you|u)\b",
    re.I,
)

_CONSENT_HINT = re.compile(
    r"\b(agree|consent|terms|conditions|confirm|acknowledge|accept|declare|"
    r"please understand|can not process|cannot process)\b",
    re.I,
)

_FIELD_TOPIC = re.compile(
    r"\b(notice|experience|ctc|salary|location|employer|relocation|pan\b|uan\b|join|available|linkedin|portfolio|phone|email|skill|years?|name|education|employer|hometown|pincode|pin\s*code|postal)\b",
    re.I,
)

_PROFILE_FIELD_LABEL = re.compile(
    r"\b(middle name|first name|last name|full name|father'?s? name|mother'?s? name|"
    r"highest level of education|education obtained|date of birth|gender|marital status|"
    r"hometown|ectc|expected ctc|current ctc|previously employed)\b",
    re.I,
)

_interactive_prompt_lock_instance: asyncio.Lock | None = None


def normalize_question_label(question: str) -> str:
    """Strip Naukri chatbot error prefix so retries match saved answers."""
    text = re.sub(r"\s+", " ", question.strip())
    return _INVALID_QUESTION_PREFIX.sub("", text).strip()


def interactive_prompt_lock() -> asyncio.Lock:
    global _interactive_prompt_lock_instance
    if _interactive_prompt_lock_instance is None:
        _interactive_prompt_lock_instance = asyncio.Lock()
    return _interactive_prompt_lock_instance


_interactive_prompt_lock = interactive_prompt_lock


def is_generic_question_label(label: str) -> bool:
    norm = re.sub(r"\s+", " ", label.strip().lower())
    return not norm or norm in GENERIC_QUESTION_LABELS or len(norm) < 3


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
    if re.match(r"^\d+\.?\d*\s*years?$", text, re.I):
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
    if re.match(r"^if your\b", text, re.I) and len(text) < 120:
        return True
    if _FIELD_TOPIC.search(text) and len(text) < 100:
        return True
    if _PROFILE_FIELD_LABEL.search(text) and len(text) < 120:
        return True
    return False
