"""Authoritative answers from config.yaml + application_facts.yaml (no LLM)."""

from __future__ import annotations

import re
from typing import Any

from ..config import AppConfig
from ..profile.application_facts import load_application_facts
from ..profile.skills import load_skill_context, skill_years_answer
from ..profile_data import load_resume_facts
from ..question_groups import _CANONICAL_SKILL_RULES, _norm, classify_question
from .compensation import (
    ctc_want_kind,
    format_lpa,
    looks_like_compensation_question,
)
from .fields import infer_field_input_type, is_numeric_ctc_question
from .location import is_location_value_question, is_relocation_yesno_question


def facts_serving_notice(config: AppConfig | None) -> bool:
    """True only when application_facts explicitly says the user is serving notice.

    A "Last Working Day" only exists while serving notice; when it is false (or
    unset) there is no valid future date to give. Shared source of truth for both
    the fill-time guard (naukri chatbot) and the saved-answer usability check so
    they never disagree (a stale LWD date must not be treated as a usable answer).
    """
    if config is None:
        return False
    try:
        facts = load_application_facts(config)
    except Exception:
        return False
    val = facts.get("serving_notice")
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "yes", "1")


def compensation_answer(config: AppConfig, question: str, field: dict[str, Any] | None = None) -> str | None:
    if not looks_like_compensation_question(question):
        return None
    want = ctc_want_kind(question)
    comp = config.compensation
    if want == "expected":
        return format_lpa(comp.expected_ctc_lpa)
    if want == "current":
        return format_lpa(comp.current_ctc_lpa)
    if want == "both":
        input_type = infer_field_input_type(question, field or {})
        if input_type == "ctc_numeric" or is_numeric_ctc_question(question):
            return f"{format_lpa(comp.current_ctc_lpa)}/{format_lpa(comp.expected_ctc_lpa)}"
        return f"Current: {format_lpa(comp.current_ctc_lpa)} LPA. Expected: {format_lpa(comp.expected_ctc_lpa)} LPA."
    return format_lpa(comp.current_ctc_lpa)


# Contact-info questions ("what is your primary email ID?", "mobile number?").
# Deterministic answers from config.user so they never fall through to the RAG/LLM
# draft path, which has no real contact data and emits garbage (resume dumps,
# arbitrary numbers like "45").
_EMAIL_Q = re.compile(r"\be-?mail\b", re.I)
_PHONE_Q = re.compile(
    r"\b(?:mobile|phone|whats\s*app|whatsapp|cell|contact|alternate|alternative)\b"
    r".{0,16}\b(?:number|no\.?|num|id)\b|\bmobile\b",
    re.I,
)
# A skill/experience question that merely mentions email/phone (rare) must not be
# answered with the user's contact value.
_NOT_CONTACT_VALUE = re.compile(r"\b(?:how many|years?|experience|do you have|have you)\b", re.I)


def contact_info_answer(config: AppConfig, question: str) -> str | None:
    """Return the user's email / phone for contact-detail questions."""
    q = question or ""
    if _NOT_CONTACT_VALUE.search(q):
        return None
    if _EMAIL_Q.search(q):
        email = (config.user.email or "").strip()
        if email:
            return email
    if _PHONE_Q.search(q):
        phone = (config.user.phone or "").strip()
        if phone:
            return phone
    return None


def location_answer(config: AppConfig, question: str) -> str | None:
    if not is_location_value_question(question):
        return None
    if is_relocation_yesno_question(question):
        return None
    app_facts = load_application_facts(config)
    if re.search(r"\bnative\b", question, re.I):
        native = str(app_facts.get("native_location", "")).strip()
        current = str(app_facts.get("current_location", "")).strip()
        if native and current:
            return f"Current: {current}; Native: {native}"
        if native:
            return native
    loc = str(app_facts.get("current_location", "")).strip()
    if loc:
        return loc
    facts = load_resume_facts(config.base_dir)
    city = facts.location.split(",")[0].strip() if facts.location else ""
    return city or None


def gender_answer(config: AppConfig) -> str | None:
    """Configured gender (application_facts.gender), used to answer gender radios."""
    app_facts = load_application_facts(config)
    g = str(app_facts.get("gender", "")).strip()
    return g or None


def profile_link_config_answer(config: AppConfig, question: str) -> str | None:
    from ..answers.profile_links import profile_link_answer

    return profile_link_answer(config, question)


def skill_years_config_answer(config: AppConfig, question: str) -> str | None:
    group_id = classify_question(question)
    if not group_id.startswith("skill:"):
        return None
    skill = group_id.split(":", 1)[1]
    facts, app_facts = load_skill_context(config)
    return skill_years_answer(config, facts, app_facts, skill.replace("_", " "))


def multi_skill_years_answer(config: AppConfig, question: str, field: dict[str, Any] | None = None) -> str | None:
    """Per-skill years for a free-text question that lists multiple skills.

    e.g. "How many years in Java, Python and AWS?" -> "Java: 4 years, Python: 4
    years, Aws: 4 years". Only fires for genuine free-text fields naming 2+ skills
    we can answer; single-skill questions fall through to skill_years_config_answer,
    and choice/multi-select fields are handled by the checkbox skill matcher.
    """
    if not _is_free_text_field(field):
        return None
    if not re.search(r"\byears?\b|\bexperience\b|\bhow many\b", question, re.I):
        return None
    norm = _norm(question)
    facts, app_facts = load_skill_context(config)
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pattern, canonical in _CANONICAL_SKILL_RULES:
        if canonical in seen or not pattern.search(norm):
            continue
        years = skill_years_answer(config, facts, app_facts, canonical.replace("_", " "))
        if years is None or not str(years).strip():
            continue
        seen.add(canonical)
        found.append((canonical.replace("_", " ").title(), str(years).strip()))
    if len(found) < 2:
        return None
    return ", ".join(f"{name}: {yrs} years" for name, yrs in found)


# High-precision "are you / have you been associated with <employer>?" detector.
# Deliberately matches "worked at/for" but NOT "worked with" (which is usually about
# a technology, e.g. "worked with Kafka"), so we never mis-answer a skill question.
_PRIOR_ASSOCIATION_Q = re.compile(
    r"\bassociated with\b"
    r"|\b(?:previously|currently)\s+employed\b"
    r"|\bemploye(?:d|e)\s+(?:of|by|with)\b"
    r"|\bworked\s+(?:at|for)\b"
    r"|\bworked for us\b|\bemployed with us\b"
    r"|\bapplied\s+(?:before|previously|earlier)\b"
    r"|\breceived an offer from\b"
    r"|\bmilitary spouse\b|\bidentify as a military\b",
    re.I,
)

_GENERIC_COMPANY_TOKENS = frozenset(
    {
        "private",
        "limited",
        "ltd",
        "pvt",
        "tech",
        "technologies",
        "technology",
        "solutions",
        "services",
        "systems",
        "software",
        "labs",
        "inc",
        "llc",
        "corp",
        "company",
        "india",
        "global",
        "innovation",
        "innovations",
        "digital",
        "consulting",
        "consultancy",
        "group",
        "enterprises",
        "ventures",
        "infotech",
    }
)


def _generic_company_tokens(config: AppConfig) -> frozenset[str]:
    """Built-in generic company-name words, extended by application_facts.

    Set ``company_generic_tokens`` in application_facts.yaml to add domain- or
    region-specific filler words (e.g. "gmbh", "labs") that should not count as a
    distinctive brand token when matching employers.
    """
    app_facts = load_application_facts(config)
    extra = app_facts.get("company_generic_tokens") or []
    if not extra:
        return _GENERIC_COMPANY_TOKENS
    return _GENERIC_COMPANY_TOKENS | {str(t).strip().lower() for t in extra if str(t).strip()}


def _known_employers(config: AppConfig) -> set[str]:
    """Employers the candidate has actually worked at — resume + optional allow-list.

    Returns full names, distinctive single-word brands, and acronyms (e.g. "Tata
    Consultancy Services" -> "tcs"), all lowercased, for substring/word matching.
    """
    names: set[str] = set()
    generic_tokens = _generic_company_tokens(config)

    def _add(raw: str) -> None:
        name = str(raw or "").strip().lower()
        if len(name) < 3:
            return
        names.add(name)
        tokens = [t for t in re.split(r"[^a-z0-9]+", name) if t]
        meaningful = [t for t in tokens if t not in generic_tokens]
        # Distinctive brand tokens only (>=4 chars) to avoid generic false matches.
        for token in meaningful:
            if len(token) >= 4:
                names.add(token)
        # Acronyms only when >=3 letters (2-letter ones match far too easily).
        if len(meaningful) >= 2:
            acronym = "".join(t[0] for t in meaningful)
            if len(acronym) >= 3:
                names.add(acronym)

    facts = load_resume_facts(config.base_dir)
    for role in facts.experience:
        _add(role.get("company", ""))

    app_facts = load_application_facts(config)
    for extra in app_facts.get("past_employers") or []:
        _add(extra)

    return names


def prior_association_answer(config: AppConfig, question: str, field: dict[str, Any] | None = None) -> str | None:
    """Deterministic Yes/No for "associated with / worked at <employer>?" questions.

    Default is "No" (you're usually not associated with the hiring company). Returns
    "Yes" only when the question names an employer you actually worked at (resume
    experience or the ``past_employers`` allow-list in application_facts.yaml).
    """
    if not _PRIOR_ASSOCIATION_Q.search(question or ""):
        return None

    q_norm = re.sub(r"[^a-z0-9]+", " ", (question or "").lower())
    q_words = set(q_norm.split())
    for emp in _known_employers(config):
        if " " in emp:
            if emp in q_norm:
                return "Yes"
        elif emp in q_words:
            return "Yes"
    return "No"


# Education / degree detector — matches a named qualification level so we can answer
# "Have you done your masters?", "Do you hold a PhD?", "Highest qualification?" etc.
_EDU_MENTION = re.compile(
    r"master'?s?\b|post[\s-]?graduat|\bpg\b|m\.?\s?tech\b|m\.?\s?sc\b|m\.?\s?e\b|"
    r"bachelor'?s?\b|under[\s-]?graduat|\bug\b|b\.?\s?tech\b|b\.?\s?sc\b|b\.?\s?e\b|"
    r"graduation\b|ph\.?\s?d\b|doctorat|\bmba\b|\bdegree\b|qualification\b|education\b",
    re.I,
)
# Phrasing that makes an education question a Yes/No (possession) question.
_EDU_YESNO_LEAD = re.compile(
    r"\b(?:have|did|do)\s+you\b|\bare\s+you\s+a\b|\bhave\s+you\s+(?:done|completed|"
    r"pursued|obtained|finished)\b|\b(?:completed|pursuing|hold|possess|done)\b",
    re.I,
)
# Phrasing for a "what is your <level>" value question.
_EDU_VALUE_LEAD = re.compile(r"\bhighest\b|\bwhat\s+is\b|\bwhich\b|\byour\s+(?:education|qualification)\b", re.I)


def _education_facts(config: AppConfig) -> dict[str, Any]:
    """Education flags, from application_facts.education or inferred from the resume."""
    app_facts = load_application_facts(config)
    resume_edu = str(load_resume_facts(config.base_dir).education or "").strip()
    edu = app_facts.get("education")
    if isinstance(edu, dict):
        highest = str(edu.get("highest", "")).strip() or resume_edu
        return {
            "highest": highest,
            "has_bachelors": bool(edu.get("has_bachelors", True)),
            "has_masters": bool(edu.get("has_masters", False)),
            "has_phd": bool(edu.get("has_phd", False)),
            "has_mba": bool(edu.get("has_mba", False)),
            "graduation_year": str(edu.get("graduation_year", "")).strip(),
            "cgpa": str(edu.get("cgpa", "")).strip(),
            "cgpa_scale": str(edu.get("cgpa_scale", "")).strip(),
            "percentage": str(edu.get("percentage", "")).strip(),
        }
    low = resume_edu.lower()
    years = re.findall(r"\b(?:19|20)\d{2}\b", resume_edu)  # graduation = latest year
    cgpa = re.search(r"(\d+(?:\.\d+)?)\s*/\s*10\b", resume_edu)
    return {
        "highest": resume_edu,
        "has_bachelors": True,
        "has_masters": bool(
            re.search(r"master|m\.?\s?tech|m\.?\s?sc|m\.?\s?e\b|post[\s-]?grad|dual degree|integrated", low)
        ),
        "has_phd": bool(re.search(r"ph\.?\s?d|doctorat", low)),
        "has_mba": bool(re.search(r"\bmba\b", low)),
        "graduation_year": (max(years) if years else ""),
        "cgpa": (cgpa.group(1) if cgpa else ""),
        "cgpa_scale": ("10" if cgpa else ""),
        "percentage": "",
    }


def education_answer(config: AppConfig, question: str, field: dict[str, Any] | None = None) -> str | None:
    """Deterministic answers for degree/qualification questions from education facts."""
    q = question or ""
    ql = q.lower()
    edu = _education_facts(config)

    # Graduation year ("year of passing", "passout year", "when did you graduate").
    if re.search(
        r"year\s+of\s+(?:passing|passout|pass\s*out|graduation|completion)|"
        r"(?:passing|passout|pass\s*out|graduation|graduating)\s+year|"
        r"when\s+did\s+you\s+(?:graduate|pass\s*out|complete)",
        ql,
    ):
        year = str(edu.get("graduation_year") or "").strip()
        if year:
            return year

    # CGPA is unambiguous; percentage/aggregate/marks only count with academic context.
    asks_cgpa = bool(re.search(r"\bc?gpa\b", ql))
    asks_pct = bool(re.search(r"percentage|aggregate|\bmarks\b|\bgrade\b", ql)) and bool(
        re.search(
            r"graduat|degree|b\.?\s?tech|btech|college|university|academic|education|qualif|10th|12th",
            ql,
        )
    )
    if asks_cgpa or asks_pct:
        cgpa = str(edu.get("cgpa") or "").strip()
        pct = str(edu.get("percentage") or "").strip()
        wants_pct = bool(re.search(r"percentage", ql))
        if wants_pct:
            # Never substitute CGPA for an explicit percentage ask — send to manual.
            if pct:
                return pct
        elif asks_cgpa:
            if cgpa:
                return cgpa
        else:
            # Generic "aggregate / marks / grade" in academic context.
            if cgpa:
                return cgpa
            if pct:
                return pct

    if not _EDU_MENTION.search(q):
        return None

    asks_masters = bool(re.search(r"master'?s?\b|post[\s-]?graduat|\bpg\b|m\.?\s?tech\b|m\.?\s?sc\b|m\.?\s?e\b", ql))
    asks_phd = bool(re.search(r"ph\.?\s?d\b|doctorat", ql))
    asks_mba = bool(re.search(r"\bmba\b", ql))
    asks_bachelors = bool(
        re.search(r"bachelor'?s?\b|under[\s-]?graduat|\bug\b|b\.?\s?tech\b|b\.?\s?sc\b|b\.?\s?e\b|graduation\b", ql)
    )
    asks_level = asks_masters or asks_phd or asks_mba or asks_bachelors

    # Yes/No possession question for a specific level.
    if asks_level and _EDU_YESNO_LEAD.search(ql):
        if asks_phd:
            return "Yes" if edu["has_phd"] else "No"
        if asks_mba:
            return "Yes" if edu["has_mba"] else "No"
        if asks_masters:
            return "Yes" if edu["has_masters"] else "No"
        if asks_bachelors:
            return "Yes" if edu["has_bachelors"] else "No"

    # "What is your highest education / qualification?" → the degree string.
    if not asks_level and _EDU_VALUE_LEAD.search(ql) and edu["highest"]:
        return edu["highest"]
    return None


# --- Compound (multi-part) free-text questions -----------------------------------
# Some recruiters cram several asks into one free-text box, e.g. "current notice
# period, current CTC and expected CTC?". The single-group classifier answers only
# one facet (CTC wins, notice is dropped). For genuine free-text fields we instead
# compose a labeled answer that addresses every facet we can derive deterministically.

_CHOICE_KINDS = {
    "radio",
    "checkbox",
    "checkbox_group",
    "single_choice",
    "multi_choice",
    "select",
    "dropdown",
}
_SINGLE_VALUE_INPUT_TYPES = {
    "ctc_numeric",
    "years_numeric",
    "number",
    "pincode",
    "date",
    "email",
    "url",
}


def _is_free_text_field(field: dict[str, Any] | None) -> bool:
    """True only for open free-text boxes — never choice or single-value inputs.

    Conservative: when we don't have field metadata (field is None) we return False,
    because emitting a multi-sentence answer into a numeric/choice control is harmful.
    """
    if not isinstance(field, dict):
        return False
    kind = str(field.get("kind", "text")).lower()
    if kind in _CHOICE_KINDS:
        return False
    input_type = str(field.get("input_type", "")).lower()
    if input_type in _SINGLE_VALUE_INPUT_TYPES:
        return False
    opts = field.get("options") or field.get("answer_options") or []
    return not len([o for o in opts if str(o).strip()]) >= 2


def _notice_segment(config: AppConfig, app_facts: dict[str, Any], norm: str) -> str | None:
    if not re.search(
        r"notice\s*period|\bnp\b|serving\s+notice|how\s+soon.*\bjoin|when\s+can\s+you\s+join|"
        r"available\s+to\s+join|join\s+immediately|joining\s+time|date\s+of\s+joining|"
        r"how\s+soon.*onboard",
        norm,
    ):
        return None
    days = app_facts.get("notice_period_days")
    if days is None:
        return None
    if app_facts.get("serving_notice") and app_facts.get("last_working_day"):
        lwd = str(app_facts["last_working_day"]).strip()
        return f"Notice period: serving notice, last working day {lwd}"
    if int(days) == 0:
        return "Notice period: 0 days (immediately available)"
    return f"Notice period: {int(days)} days"


def _lwd_segment(app_facts: dict[str, Any], norm: str) -> str | None:
    if not re.search(r"last\s+working\s+day|\blwd\b", norm):
        return None
    if app_facts.get("serving_notice"):
        return None  # already covered by the notice segment
    lwd = str(app_facts.get("last_working_day", "")).strip()
    return f"Last working day: {lwd}" if lwd else None


def _ctc_segment(config: AppConfig, question: str, norm: str) -> str | None:
    if not looks_like_compensation_question(question):
        return None
    comp = config.compensation
    want = ctc_want_kind(question)
    cur = format_lpa(comp.current_ctc_lpa)
    exp = format_lpa(comp.expected_ctc_lpa)
    if want == "current":
        return f"Current CTC: {cur} LPA"
    if want == "expected":
        return f"Expected CTC: {exp} LPA"
    return f"Current CTC: {cur} LPA, Expected CTC: {exp} LPA"


def _reason_segment(app_facts: dict[str, Any], norm: str) -> str | None:
    if not re.search(
        r"reason\s+for\s+(change|leaving|looking|job\s*change)|why.*\b(looking|change|leave)\b|"
        r"reason\s+for\s+job\s+change",
        norm,
    ):
        return None
    reason = str(app_facts.get("reason_for_change", "")).strip()
    return f"Reason for change: {reason}" if reason else None


def compound_config_answer(
    config: AppConfig,
    question: str,
    field: dict[str, Any] | None = None,
) -> str | None:
    """Compose a labeled answer for free-text questions that ask 2+ things at once.

    Returns None unless the field is genuine free text AND at least two distinct
    facets each resolve deterministically — otherwise the normal single-facet
    answerers (or the LLM) handle it.
    """
    if not _is_free_text_field(field):
        return None
    norm = question.lower()
    app_facts = load_application_facts(config)
    segments = [
        _notice_segment(config, app_facts, norm),
        _lwd_segment(app_facts, norm),
        _ctc_segment(config, question, norm),
        _reason_segment(app_facts, norm),
    ]
    resolved = [s.rstrip(". ").strip() for s in segments if s]
    if len(resolved) < 2:
        return None
    return ". ".join(resolved) + "."


def authoritative_config_answer(
    config: AppConfig,
    question: str,
    field: dict[str, Any] | None = None,
) -> str | None:
    """Contact → compound → compensation → location → profile links → skill years → prior association → education."""
    for answer in (
        contact_info_answer(config, question),
        compound_config_answer(config, question, field),
        compensation_answer(config, question, field),
        location_answer(config, question),
        profile_link_config_answer(config, question),
        multi_skill_years_answer(config, question, field),
        skill_years_config_answer(config, question),
        prior_association_answer(config, question, field),
        education_answer(config, question, field),
    ):
        if answer is not None and str(answer).strip():
            return answer
    return None
