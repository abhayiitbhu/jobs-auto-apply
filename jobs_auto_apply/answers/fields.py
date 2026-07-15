from __future__ import annotations

import re
from typing import Any

from ..config import AppConfig
from .location import is_location_value_question

_PINCODE_FIELD = re.compile(r"\b(pin\s*code|pincode|zip\s*code|postal\s*code)\b", re.I)

_NUMERIC_CTC_Q = re.compile(
    r"\b(?:current|expected|your)\s+.{0,24}\bctc\b.{0,32}\b(?:lacs?|lakhs?|lpa|inr)\b|"
    r"\bctc\b.{0,32}\b(?:in\s+)?(?:lacs?|lakhs?|lpa|inr)\b|"
    r"\b(?:annual|expected|current)\s+.{0,16}\bctc\b|"
    r"\b(?:lacs?|lakhs?)\s+per\s*annum\b|"
    r"\bectc\b",
    re.I,
)

_OPTIONAL_NAME_FIELD = re.compile(
    r"\b(middle name|first name|last name|maiden name|nick\s*name|nickname)\b",
    re.I,
)
OPTIONAL_NAME_FIELD = _OPTIONAL_NAME_FIELD


def is_pincode_field(label: str) -> bool:
    return bool(_PINCODE_FIELD.search(label))


def is_numeric_ctc_question(label: str) -> bool:
    norm = label.lower()
    if re.search(r"\b(cctc|ectc)\b", norm):
        return True
    if not re.search(r"\b(ctc|salary|compensation)\b", norm):
        return False
    # "Salary expectation(s)" / "expected salary" on Indian portals means a numeric
    # (LPA) figure even without an explicit LPA/lacs marker.
    if re.search(r"salary expectations?|expected salary|salary you (?:are )?expecting", norm):
        return True
    if re.search(r"\blpa\b", norm):
        return True
    if re.search(r"\bannual salary\b", norm):
        return True
    if _NUMERIC_CTC_Q.search(norm):
        return True
    return bool(re.search(r"\bin\s+lacs?\b", norm))


def is_last_working_day_question(question: str) -> bool:
    from ..question_groups import classify_question

    if classify_question(question) == "last_working_day":
        return True
    return bool(re.search(r"\b(last working day|lwd)\b", question, re.I))


def _label_implies_years_numeric(label: str) -> bool:
    norm = label.lower()
    # "Do you have experience in X?" / "Have you ... experience in X?" with no
    # "how many/much" and no "years" is a yes/no question, not a numeric-years
    # one — don't let the broad "experience ... in" match below mislabel it.
    if re.search(r"\b(do you have|have you)\b", norm) and not re.search(r"\bhow (many|much)\b|\byears?\b", norm):
        return False
    if re.search(
        r"\bhow (many|much)\b.*\bexperience\b|\bexperience\b.*\bin\b",
        norm,
    ):
        return True
    return bool(
        re.search(
            r"\bhow many\b.*\byears?\b|\byears?\b.*\bexperience\b|"
            r"^experience in\b|\bai domains?\b|knowledge of hyperscalers",
            norm,
        )
    )


def infer_field_input_type(label: str, field: dict[str, Any] | None = None) -> str:
    """Infer the value shape the UI expects (numeric CTC, years, date, choice, etc.)."""
    field = field or {}
    explicit = str(field.get("input_type", "")).strip()
    kind = str(field.get("kind", "text"))

    if _label_implies_years_numeric(label) and kind in (
        "text",
        "input",
        "textarea",
        "number",
    ):
        return "years_numeric"
    placeholder = str(field.get("placeholder", ""))
    input_mode = str(field.get("input_mode", "")).lower()

    if is_pincode_field(label):
        return "pincode"
    if is_location_value_question(label):
        return "location"
    if is_last_working_day_question(label):
        return "date"
    if re.search(
        r"\b(desired start date|start date|date of joining|joining date|earliest start|available from)\b",
        label,
        re.I,
    ):
        return "date"
    if is_numeric_ctc_question(label) or (placeholder and re.search(r"lakh|lac", placeholder, re.I)):
        return "ctc_numeric"
    if re.search(r"\b(date of birth|dob|birth date)\b", label, re.I) or field.get("hasDobInput"):
        return "date"
    if input_mode == "number" or kind == "number":
        return "number"
    if kind == "radio" and field.get("options"):
        opts = {str(o).strip().lower() for o in field.get("options", []) if str(o).strip()}
        if opts <= {"yes", "no"} and opts:
            return "single_choice"
    if re.search(r"\b(do you have|have you)\b", label, re.I) and not re.search(r"\bhow (many|much)\b", label, re.I):
        if (
            re.search(r"\b(experience|using|worked with|hands?.on)\b", label, re.I)
            and re.search(
                r"\b(genai|llm|copilot|dataiku|ai/ml|ml models?)\b",
                label,
                re.I,
            )
            and (kind in ("text", "input", "textarea", "number") or field.get("hasVisibleInput"))
        ):
            return "years_numeric"
    if _label_implies_years_numeric(label):
        if kind in ("text", "input", "textarea", "number") or field.get("hasVisibleInput"):
            return "years_numeric"
    if explicit:
        return explicit
    if kind == "radio" and field.get("options"):
        return "single_choice"
    if kind == "checkbox_group" and field.get("options"):
        return "multi_choice"
    if kind == "checkbox":
        return "yes_no_checkbox"
    if kind in ("text", "input", "textarea"):
        return "short_text"
    return kind or "text"


def enrich_field_for_llm(field: dict[str, Any]) -> dict[str, Any]:
    """Attach input_type and keep discovery metadata for LLM prompts."""
    enriched = dict(field)
    label = str(field.get("label", "")).strip()
    enriched["input_type"] = infer_field_input_type(label, enriched)
    return enriched


_FREE_TEXT_INPUT_TYPES = frozenset({"short_text", "long_text", "contenteditable", "text"})


def is_free_text_field(field: dict[str, Any]) -> bool:
    """True for open-ended text fields (not choice/numeric/date/location/etc.).

    These are the answers the user wants to verify manually rather than auto-fill.
    """
    enriched = field if field.get("input_type") else enrich_field_for_llm(field)
    return str(enriched.get("input_type", "")).lower() in _FREE_TEXT_INPUT_TYPES


def infer_field_for_question(question: str, config: AppConfig | None = None) -> dict[str, Any]:
    """Best-effort field metadata when options aren't known (e.g. pending queue)."""
    label = question.strip()
    q = label.lower()
    chip_options = (
        list(config.answers.default_year_chip_options)
        if config is not None
        else ["No experience", "<6 years", "6-8 years", "8+ years"]
    )
    if re.search(r"\bhow many\b.*\byears?\b|\byears?\b.*\bexperience\b", q):
        return {
            "kind": "radio",
            "label": label,
            "options": chip_options,
        }
    if re.search(r"\b(do you have|have you).*\b(experience|using|worked with|hands?.on)\b", q) and re.search(
        r"\b(genai|llm|copilot|dataiku|ai/ml|ml models?|artificial intelligence)\b",
        q,
        re.I,
    ):
        return {"kind": "text", "label": label, "input_type": "years_numeric"}
    if re.search(
        r"\b(do you have|have you|are you|do you|any offers?|holding any offer|"
        r"received an offer|currently have|previously uploaded|interview attended|"
        r"applied before|can not process)\b",
        q,
    ):
        return {"kind": "radio", "label": label, "options": ["Yes", "No"]}
    if re.search(r"\b(date of birth|dob|birth date)\b", q):
        return {"kind": "text", "label": label, "input_type": "date"}
    if is_last_working_day_question(label):
        return {"kind": "text", "label": label, "input_type": "date"}
    if is_pincode_field(label):
        return {"kind": "text", "label": label, "input_type": "pincode"}
    if is_location_value_question(label):
        return {"kind": "text", "label": label, "input_type": "location"}
    if is_numeric_ctc_question(label):
        return {"kind": "text", "label": label, "input_type": "ctc_numeric"}
    if re.search(r"\b(yes|no|available|willing|employed|associated|immediate|offer)\b", q):
        return {"kind": "radio", "label": label, "options": ["Yes", "No"]}
    return {"kind": "text", "label": label}
