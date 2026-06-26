"""CTC / compensation detection, formatting, and numeric resolution."""

from __future__ import annotations

import re

from ..config import AppConfig
from ..question_groups import classify_question
from .fields import is_numeric_ctc_question


def format_lpa(value: float) -> str:
    n = float(value)
    if n == int(n):
        return str(int(n))
    return f"{n:g}"


def ctc_want_kind(question: str) -> str:
    norm = question.lower()
    has_current = bool(re.search(r"\bcurrent\b", norm))
    has_expected = bool(
        re.search(
            r"\b(expected|expecting|ectc|salary expectation|expected salary|"
            r"salary are you expecting|annual salary are you expecting)\b",
            norm,
        )
    )
    if has_expected and not has_current:
        return "expected"
    if has_current and not has_expected:
        return "current"
    if has_expected and has_current:
        return "both"
    if re.search(
        r"\b(salary expectations?|expected salary|how much annual salary)\b",
        norm,
    ):
        return "expected"
    if re.search(r"\bectc\b", norm):
        return "expected"
    return "current"


def looks_like_compensation_question(label: str) -> bool:
    if classify_question(label) == "compensation":
        return True
    return bool(re.search(r"\b(ctc|salary|lpa|compensation|ectc|cctc)\b", label, re.I))


def compensation_answer_compatible(question: str, answer: str) -> bool:
    """Reject cross-reuse of current-only answers for expected CTC (and vice versa)."""
    if classify_question(question) != "compensation":
        return True
    want = ctc_want_kind(question)
    text = answer.strip()
    if not text:
        return False
    lower = text.lower()
    has_current = bool(re.search(r"\bcurrent\b", lower))
    has_expected = bool(re.search(r"\b(expected|expecting)\b", lower))

    if want == "expected":
        if has_current:
            return False
        if re.search(r"\bfixed\b|\bvariable\b|\besop", lower):
            return False
        if re.fullmatch(r"\d+(?:\.\d+)?(?:\s*lpa)?", text, re.I):
            return True
        if has_expected:
            return True
        if re.search(r"\d+\s*lpa", text, re.I):
            return True
        return True
    if want == "current":
        if has_expected and not has_current:
            return False
        if re.fullmatch(r"\d+(?:\.\d+)?(?:\s*lpa)?", text, re.I):
            return True
        if has_current:
            return True
        return not has_expected
    if want == "both":
        if re.fullmatch(r"\d+(?:\.\d+)?/\d+(?:\.\d+)?", text):
            return True
        return has_current and has_expected
    return True


def _format_lpa(value: float) -> str:
    return format_lpa(value)


def resolve_ctc_numeric_answer(
    question: str,
    answer: str,
    config: AppConfig | None = None,
) -> str | None:
    """Map CTC questions to a numeric lakhs value for Naukri-style numeric fields."""
    if not is_numeric_ctc_question(question):
        return None
    want = ctc_want_kind(question)
    text = answer.strip()
    if not text:
        return None

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return text

    if want == "expected":
        if re.search(r"\bcurrent\b", text, re.I) and not re.search(r"\bexpected\b", text, re.I):
            if config:
                return _format_lpa(config.compensation.expected_ctc_lpa)
            return None
        for pattern in (
            r"expected[:\s]*(?:₹?\s*)?(\d+(?:\.\d+)?)",
            r"expecting\s+(?:around\s+)?(\d+(?:\.\d+)?)",
            r"(\d+(?:\.\d+)?)\s*lpa",
        ):
            match = re.search(pattern, text, re.I)
            if match:
                return match.group(1)
        if config:
            return _format_lpa(config.compensation.expected_ctc_lpa)

    if want == "current":
        for pattern in (
            r"current[:\s]*(?:₹?\s*)?(\d+(?:\.\d+)?)",
            r"^(\d+(?:\.\d+)?)\s*lpa",
            r"^(\d+(?:\.\d+)?)\s*(?:l\s+fixed|lakhs?)",
        ):
            match = re.search(pattern, text, re.I)
            if match:
                return match.group(1)
        if config:
            return _format_lpa(config.compensation.current_ctc_lpa)

    if want == "both":
        if re.fullmatch(r"\d+(?:\.\d+)?/\d+(?:\.\d+)?", text.strip()):
            return text.strip()
        current = expected = ""
        current_match = re.search(r"current[:\s]*(?:₹?\s*)?(\d+(?:\.\d+)?)", text, re.I)
        expected_match = re.search(r"expected[:\s]*(?:₹?\s*)?(\d+(?:\.\d+)?)", text, re.I)
        if current_match:
            current = current_match.group(1)
        if expected_match:
            expected = expected_match.group(1)
        if current and expected:
            return f"{current}/{expected}"
        if config:
            comp = config.compensation
            return f"{_format_lpa(comp.current_ctc_lpa)}/{_format_lpa(comp.expected_ctc_lpa)}"

    nums = re.findall(r"(\d+(?:\.\d+)?)", text)
    if want == "expected" and nums:
        if re.search(r"\bexpected\b", text, re.I):
            exp = re.search(r"expected[:\s]*(?:₹?\s*)?(\d+(?:\.\d+)?)", text, re.I)
            if exp:
                return exp.group(1)
            return nums[-1]
        if not re.search(r"\bcurrent\b", text, re.I):
            return nums[-1]
    if want == "current" and nums:
        if re.search(r"\bcurrent\b", text, re.I):
            cur = re.search(r"current[:\s]*(?:₹?\s*)?(\d+(?:\.\d+)?)", text, re.I)
            if cur:
                return cur.group(1)
            return nums[0]
        if not re.search(r"\bexpected\b", text, re.I):
            return nums[0]
    if config:
        comp = config.compensation
        if want == "expected":
            return _format_lpa(comp.expected_ctc_lpa)
        return _format_lpa(comp.current_ctc_lpa)
    return None


# Private aliases
_looks_like_compensation_question = looks_like_compensation_question
_compensation_answer_compatible = compensation_answer_compatible
