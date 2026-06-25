from __future__ import annotations

import logging
import re
from typing import Any

from .config import AppConfig
from .profile_data import ResumeFacts, load_resume_facts
from .profile.application_facts import load_application_facts
from .profile.skills import (
    has_skill,
    skill_experience_configured,
    skill_years_answer,
    skill_years_override,
)
from .answers.profile_links import profile_link_answer
from .answers.text import norm_text
from .question_groups import classify_question
from .resume_text import load_resume_text, resume_paragraphs

logger = logging.getLogger("job_apply")

_CONSENT_LABEL = re.compile(
    r"\b(agree|consent|terms|conditions|confirm|acknowledge|accept|declare)\b",
    re.I,
)

# Yes/No-style phrasing. Naukri sometimes renders these as a free-text composer on
# the first discovery pass; without this guard the resume-blob fallback dumps an
# unrelated paragraph as the "answer" instead of deferring to a real Yes/No.
_YES_NO_PHRASING = re.compile(
    r"^\s*(are|do|did|does|have|has|had|will|would|can|could|is|was|were|should)\s+you\b",
    re.I,
)
# Prompts that explicitly ask for free-text elaboration — these are NOT pure yes/no
# even when they open with "Have you …", so they should still get a text answer.
_WANTS_ELABORATION = re.compile(
    r"\b(describe|explain|elaborate|detail|details|specify|share|tell us|"
    r"walk (?:me|us) through|give (?:an )?examples?|list|which|what|how|why)\b",
    re.I,
)


def _looks_like_pure_yes_no(question: str) -> bool:
    return bool(
        _YES_NO_PHRASING.search(question) and not _WANTS_ELABORATION.search(question)
    )


def _inline_choice_options(question: str) -> list[str]:
    """Options written inline in the label, e.g. "(Beginner/Intermediate/Advanced)".

    Naukri/Hirist sometimes render these rating/scale questions as a plain text box
    (no real choice control), so discovery never captures the options. Without this
    the resume-blob fallback answers a "How strong are you …?" scale question with an
    unrelated paragraph. We parse the parenthetical slash-list so the caller can defer
    to the LLM (which sees the options and picks one) instead.
    """
    for group in re.findall(r"\(([^)]+)\)", question):
        if "/" not in group:
            continue
        parts = [p.strip() for p in group.split("/") if p.strip()]
        if len(parts) < 2:
            continue
        # Reject numeric/example parentheticals like "(in LPA, e.g., 45)".
        if any(re.search(r"\d", p) or len(p) > 24 for p in parts):
            continue
        if all(re.search(r"[A-Za-z]", p) for p in parts):
            return parts
    return []


def _norm(text: str) -> str:
    return norm_text(text)


def _tokens(text: str) -> set[str]:
    stop = {
        "what", "your", "you", "are", "the", "a", "an", "do", "have", "in", "of",
        "for", "this", "role", "how", "many", "years", "experience", "with", "is",
        "and", "or", "if", "yes", "can", "describe", "briefly", "please",
    }
    return {w for w in _norm(text).split() if len(w) > 2 and w not in stop}


def _retrieve_chunks(
    question: str,
    jd: str,
    facts: ResumeFacts,
    config: AppConfig | None = None,
) -> list[str]:
    """Score resume/JD snippets by token overlap with the question."""
    query = _tokens(question) | _tokens(jd[:2000])
    if not query:
        return []

    candidates: list[tuple[float, str]] = []
    candidates.append((0.2, facts.profile_summary.strip()))

    for role in facts.experience:
        company = str(role.get("company", ""))
        title = str(role.get("title", ""))
        header = f"{title} at {company}"
        candidates.append((0.15, header))
        for highlight in role.get("highlights", []):
            candidates.append((0.0, str(highlight)))

    for skill_group in facts.technical_skills.values():
        candidates.append((0.1, ", ".join(skill_group)))

    if config is not None:
        for para in resume_paragraphs(config):
            candidates.append((0.05, para))

    scored: list[tuple[float, str]] = []
    for base, text in candidates:
        if not text:
            continue
        text_tokens = _tokens(text)
        if not text_tokens:
            continue
        overlap = len(query & text_tokens) / max(len(query), 1)
        scored.append((base + overlap, text))

    scored.sort(key=lambda x: x[0], reverse=True)
    seen: set[str] = set()
    chunks: list[str] = []
    for score, text in scored:
        if score < 0.12 or text in seen:
            continue
        seen.add(text)
        chunks.append(text)
        if len(chunks) >= 4:
            break
    return chunks


def _resume_blob(facts: ResumeFacts, config: AppConfig | None = None) -> str:
    from .profile.skills import resume_blob

    return resume_blob(facts, config)


# When a yes/no question names specific tools, every named tool must appear in the resume.
_COMPOUND_TECH_CHECKS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\bglue\b"), ("glue", "aws glue")),
    (re.compile(r"\bredshift\b"), ("redshift",)),
    (re.compile(r"\blambda\b"), ("lambda", "aws lambda")),
    (re.compile(r"\bsnowflake\b"), ("snowflake", "snow sql")),
    (re.compile(r"\bdatabricks\b"), ("databricks",)),
    (re.compile(r"\bairflow\b"), ("airflow",)),
    (re.compile(r"\bterraform\b"), ("terraform",)),
    (re.compile(r"\bkubernetes\b|\bk8s\b"), ("kubernetes", "k8s")),
    (re.compile(r"\breact\b"), ("react", "react.js", "reactjs")),
    (re.compile(r"\bangular\b"), ("angular", "angularjs")),
    (re.compile(r"\bscala\b"), ("scala",)),
    (re.compile(r"\bgolang\b|\bgo\b"), (" golang", " go ", "golang")),
    (re.compile(r"\bazure\b"), ("azure",)),
    (re.compile(r"\bgcp\b|google cloud"), ("gcp", "google cloud", "bigquery")),
    (re.compile(r"langchain"), ("langchain",)),
    (re.compile(r"\bnlp\b"), ("nlp", "natural language")),
    (re.compile(r"computer vision"), ("computer vision", "opencv")),
    (re.compile(r"pytorch|tensorflow"), ("pytorch", "tensorflow")),
)


def _mentioned_technologies(norm: str) -> list[str]:
    found: list[str] = []
    for pattern, _keywords in _COMPOUND_TECH_CHECKS:
        if pattern.search(norm):
            found.append(pattern.pattern)
    return found


def _has_named_technologies(norm: str, blob: str) -> bool:
    """All technologies explicitly named in the question must appear in resume text."""
    checks: list[tuple[str, ...]] = []
    for pattern, keywords in _COMPOUND_TECH_CHECKS:
        if pattern.search(norm):
            checks.append(keywords)
    if not checks:
        return True
    return all(any(kw in blob for kw in keywords) for keywords in checks)


def _skill_yesno_has(
    question: str,
    facts: ResumeFacts,
    config: AppConfig,
    skill: str,
    app_facts: dict[str, Any] | None = None,
) -> bool:
    """Yes only when resume supports the skill AND every technology named in the question."""
    norm = _norm(question)
    blob = _resume_blob(facts, config)
    if not _has_named_technologies(norm, blob):
        return False
    return has_skill(facts, skill, config, app_facts)


def _skill_yesno_decision(
    question: str,
    facts: ResumeFacts,
    config: AppConfig,
    skill: str,
    app_facts: dict[str, Any] | None = None,
) -> bool | None:
    """True/False only when we have grounds; None for a NEW/unknown skill.

    We never auto-answer "No" for a skill the user has not declared (in
    ``skill_years``) and that is not configured/in the resume — guessing "No"
    on a skill we know nothing about is wrong, so we defer it (the LLM/manual
    path can decide instead).
    """
    skill_label = skill.replace("_", " ")
    declared = skill_years_override(app_facts or {}, skill_label)
    if declared is not None:
        return declared > 0
    if skill_experience_configured(config, facts, app_facts or {}, skill_label):
        return _skill_yesno_has(question, facts, config, skill, app_facts)
    return None


def _expects_short_answer(question: str) -> bool:
    norm = _norm(question)
    return bool(
        re.search(
            r"how many|years|ctc|salary|usd|monthly|linkedin|url|when would|"
            r"available to start|rate your|comfortable|based in india|cet working|"
            r"associated with|previously employed|military spouse|identify as|"
            r"profile previously uploaded|interview attended|can not process|"
            r"\boffers?\b|date of birth|dob|birth date|pin\s*code|pincode|postal\s*code|"
            r"^experience in\b|\bai domains?\b|knowledge of hyperscalers",
            norm,
        )
    )


def _cities_from_question_label(question: str) -> list[str]:
    """Parse city names from labels like 'Preferred Location (Bengaluru/Trivandrum)'."""
    match = re.search(r"\(([^)]+)\)", question)
    if not match:
        return []
    return [part.strip() for part in re.split(r"[/,|]", match.group(1)) if part.strip()]


def _city_name_matches(a: str, b: str) -> bool:
    x, y = a.strip().lower(), b.strip().lower()
    if not x or not y:
        return False
    if x == y or x in y or y in x:
        return True
    aliases = {
        "bengaluru": ("bangalore", "bengaluru"),
        "bangalore": ("bangalore", "bengaluru"),
        "gurugram": ("gurgaon", "gurugram"),
        "gurgaon": ("gurgaon", "gurugram"),
    }
    for left, rights in aliases.items():
        if x == left and any(r in y for r in rights):
            return True
        if y == left and any(r in x for r in rights):
            return True
    return False


def _worked_at_company(facts: ResumeFacts, company_hint: str) -> bool:
    hint = company_hint.strip().lower()
    if not hint:
        return False
    for role in facts.experience:
        company = str(role.get("company", "")).lower()
        if hint in company or company in hint:
            return True
    return False


def _pick_negative_option(options: list[Any]) -> str | None:
    for opt in options:
        text = str(opt).strip()
        if re.search(r"\b(not|never|no)\b", text, re.I):
            return text
    for opt in options:
        if str(opt).strip().lower() == "no":
            return str(opt)
    return None


def _pick_positive_option(options: list[Any]) -> str | None:
    for opt in options:
        text = str(opt).strip()
        if re.search(r"\b(yes|currently|previously)\b", text, re.I) and not re.search(
            r"\b(not|never|no)\b", text, re.I
        ):
            return text
    for opt in options:
        if str(opt).strip().lower() == "yes":
            return str(opt)
    return None


def _compose_text_answer(question: str, chunks: list[str], facts: ResumeFacts) -> str:
    if chunks:
        text = ". ".join(s.strip().rstrip(".") for s in chunks[:2]) + "."
    else:
        text = facts.recent_role_blurb()
    if len(text) > 480:
        text = text[:477].rsplit(" ", 1)[0] + "..."
    return text


def _current_ctc_answer(config: AppConfig, question: str) -> str:
    comp = config.compensation
    norm = _norm(question)

    def _fmt_lpa(value: float) -> str:
        n = float(value)
        if n == int(n):
            return str(int(n))
        return f"{n:g}"

    current = _fmt_lpa(comp.current_ctc_lpa)
    expected = _fmt_lpa(comp.expected_ctc_lpa)
    numeric_field = bool(
        re.search(r"\b(lacs?|lakhs?|lpa|per\s*annum|p\.?a\.?)\b", norm)
        or re.search(r"\bin\s+lacs?\b", norm)
    )

    if "expected" in norm and "current" not in norm:
        return expected
    if re.search(r"\bexpecting\b", norm) and "current" not in norm:
        return expected
    if "current" in norm and "expected" not in norm:
        return current
    if "salary expectation" in norm:
        return expected
    if "expected" in norm and "current" in norm:
        if numeric_field or not re.search(r"\b(describ|explain|detail|breakdown)\b", norm):
            return f"{current}/{expected}"
        return f"Current: {current} LPA. Expected: {expected} LPA."
    if numeric_field:
        return current
    return f"Current: {current} LPA. Expected: {expected} LPA."


def _checkbox_group_from_skills(
    options: list[Any],
    facts: ResumeFacts,
    config: AppConfig | None = None,
) -> str | None:
    matched: list[str] = []
    for opt in options:
        opt_s = str(opt).strip()
        if not opt_s:
            continue
        if has_skill(facts, opt_s, config):
            matched.append(opt_s)
    return ", ".join(matched) if matched else None


def _yes_no_option(options: list[Any]) -> bool:
    opts = {str(o).strip().lower() for o in options if str(o).strip()}
    return opts.issubset({"yes", "no"}) and len(opts) >= 2


def _yes_no_radio_answer(
    question: str,
    app_facts: dict[str, Any],
    norm: str,
    config: AppConfig | None = None,
) -> str | None:
    from .answer_suggest import is_prior_application_screening

    if is_prior_application_screening(question):
        val = app_facts.get("previously_applied_to_employer", False)
        return "Yes" if val else "No"
    if re.search(r"join.*(immediately|within\s*15)|within\s*15\s*days|15\s*days", norm):
        days = app_facts.get("notice_period_days")
        threshold = (
            config.answers.notice_join_threshold_days if config is not None else 15
        )
        if days is not None:
            return "Yes" if int(days) <= threshold else "No"
    if re.search(
        r"\bf2f\b|face[\s-]?to[\s-]?face|final.{0,24}(f2f|face)|(f2f|face).{0,24}final",
        norm,
    ):
        return str(app_facts.get("f2f_interview_available", "No"))
    if re.search(r"comfortable|willing|work from office|\bwfo\b|relocat|gurgaon|gurugram|bangalore|bengaluru|hyderabad|mumbai|pune|delhi|ncr", norm):
        return str(app_facts.get("willing_to_relocate", "Yes"))
    if re.search(r"pf|provident|insurance|employment package", norm):
        return "Yes"
    if re.search(
        r"\b(available|attend|schedule).{0,40}\binterview\b|"
        r"\binterview\b.{0,40}\b(available|attend|schedule|virtual|face)",
        norm,
    ):
        return str(app_facts.get("interview_available", "Yes"))
    if re.search(r"\b(offers?|holding.*offer|job offer)\b", norm):
        val = app_facts.get("has_job_offers")
        if val is not None:
            return "Yes" if str(val).lower() in ("yes", "true", "1") else "No"
        return "No"
    return None


def generate_rag_answer(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    jd: str = "",
    job_title: str = "",
) -> str | None:
    """
    Retrieval-augmented answer from profile/application_facts + resume + compensation.
    Returns None when facts are missing (e.g. PAN not configured).
    """
    facts = load_resume_facts(config.base_dir)
    app_facts = load_application_facts(config)
    group_id = classify_question(question)
    norm = _norm(question)
    years = str(config.profile.years_experience)
    kind = str(field.get("kind", "text"))
    options = [str(o) for o in field.get("options", []) if str(o).strip()]

    from .answer_suggest import is_prior_application_screening

    if group_id == "prior_application" or is_prior_application_screening(question):
        val = app_facts.get("previously_applied_to_employer", False)
        if kind in ("radio", "checkbox") and options:
            picked = _pick_positive_option(options) if val else _pick_negative_option(options)
            if picked:
                return picked
        return "Yes" if val else "No"

    if group_id == "pincode" or re.search(r"\b(pin\s*code|pincode|zip\s*code|postal\s*code)\b", norm):
        pin = str(app_facts.get("pincode", "")).strip()
        if not pin:
            pin = str(config.workday.address.postal_code or "").strip()
        return pin or None

    if re.search(r"\bmilitary spouse\b", norm):
        if kind in ("radio", "checkbox") and options:
            picked = _pick_negative_option(options)
            if picked:
                return picked
        return "No"

    if re.search(
        r"\b(associated with|previously employed|currently employed at|employee of|"
        r"employed by|worked (?:at|for|with)|previously worked|employed with us|"
        r"worked for us|applied previously|received an offer from|previously or are currently)\b",
        norm,
    ):
        company_hint = ""
        for pattern in (
            r"associated with\s+(.+?)\??$",
            r"employed (?:at|by|with)\s+(.+?)\??$",
            r"worked (?:at|for|with)\s+(.+?)\??$",
            r"received an offer from\s+(.+?)\??$",
        ):
            match = re.search(pattern, norm, re.I)
            if match:
                company_hint = match.group(1).strip()
                break
        if not company_hint:
            for token in re.findall(r"[a-z0-9]+", norm):
                if len(token) >= 3 and token not in {
                    "have", "been", "previously", "currently", "associated", "with",
                    "employed", "worked", "you", "your", "ever", "any", "the", "our",
                    "us", "for", "are", "was", "did",
                }:
                    company_hint = token
                    break
        worked = _worked_at_company(facts, company_hint)
        if kind in ("radio", "checkbox") and options:
            picked = _pick_positive_option(options) if worked else _pick_negative_option(options)
            if picked:
                return picked
            return "Yes" if worked else "No"
        return "Yes" if worked else "No"

    if group_id == "join_availability":
        if kind in ("radio", "checkbox") and (_yes_no_option(options) or not options):
            yn = _yes_no_radio_answer(question, app_facts, norm, config)
            if yn:
                return yn
            days = app_facts.get("notice_period_days")
            if days is not None:
                threshold = config.answers.notice_join_threshold_days
                return "Yes" if int(days) <= threshold else "No"
        return None

    if kind == "checkbox":
        if _CONSENT_LABEL.search(question):
            return "Yes"
        if group_id.startswith("skill_yesno:"):
            skill = group_id.split(":", 1)[1]
            decision = _skill_yesno_decision(question, facts, config, skill, app_facts)
            if decision is None:
                return None
            return "Yes" if decision else "No"
        return "Yes"

    if kind == "radio" and (_yes_no_option(options) or not options):
        yn = _yes_no_radio_answer(question, app_facts, norm, config)
        if yn and yn.lower() in ("yes", "no"):
            return yn
        if group_id.startswith("skill_yesno:"):
            skill = group_id.split(":", 1)[1]
            decision = _skill_yesno_decision(question, facts, config, skill, app_facts)
            if decision is None:
                return None
            return "Yes" if decision else "No"
        if "comfortable" in norm or "willing" in norm:
            return str(app_facts.get("willing_to_relocate", "Yes"))
        if re.search(
            r"\b(associated with|previously employed|employee of|military spouse)\b",
            norm,
        ):
            return None
        return None

    if kind == "checkbox_group" and options:
        skill_pick = _checkbox_group_from_skills(options, facts, config)
        if skill_pick:
            return skill_pick
        if group_id == "preferred_location":
            prefs = [str(p).lower() for p in app_facts.get("preferred_locations", [])]
            picked = [
                opt for opt in options
                if any(pref in str(opt).lower() or str(opt).lower() in pref for pref in prefs)
            ]
            if picked:
                return ", ".join(picked)
        if group_id.startswith("skill_yesno:"):
            skill = group_id.split(":", 1)[1]
            decision = _skill_yesno_decision(question, facts, config, skill, app_facts)
            if decision is None:
                return None
            if decision:
                return ", ".join(
                    opt for opt in options
                    if has_skill(facts, str(opt), config, app_facts) or skill in str(opt).lower()
                ) or options[0]
            return "No"
        if len(options) == 1:
            return options[0]
        return "all"

    if group_id == "compensation":
        return _current_ctc_answer(config, question)

    if group_id == "last_working_day" or re.search(
        r"\b(last working day|lwd)\b", norm
    ):
        lwd = str(app_facts.get("last_working_day", "")).strip()
        if lwd:
            return lwd
        return None

    if group_id == "f2f_interview":
        return str(app_facts.get("f2f_interview_available", "No"))

    if group_id == "notice_period":
        days = app_facts.get("notice_period_days")
        if days is None:
            return None
        if app_facts.get("serving_notice") and app_facts.get("last_working_day"):
            lwd = str(app_facts["last_working_day"]).strip()
            if re.search(r"\b(last working day|lwd)\b", norm):
                return lwd
            return f"Serving notice period. Last working day: {lwd}."
        if int(days) == 0:
            if re.search(r"notice period|notice in days|\bdays\b", norm):
                return "0"
            return "Immediately available"
        if "days" in norm:
            return str(days)
        return f"{days} days"

    if group_id == "current_location":
        current = str(app_facts.get("current_location", "")).strip()
        if not current:
            current = facts.location.split(",")[0].strip()
        if re.search(r"\bnative\b", norm):
            native = str(app_facts.get("native_location", "")).strip()
            if native:
                return f"Current: {current}; Native: {native}"
        return current or facts.location

    if group_id == "preferred_location":
        current = str(app_facts.get("current_location", "")).strip()
        if not current:
            current = facts.location.split(",")[0].strip()
        listed_cities = _cities_from_question_label(question)
        if listed_cities:
            for city in listed_cities:
                if _city_name_matches(city, current):
                    if kind in ("radio", "checkbox_group") and options:
                        for opt in options:
                            if _city_name_matches(city, str(opt)):
                                return str(opt)
                    return city
            if kind in ("radio", "checkbox_group") and options:
                for opt in options:
                    for city in listed_cities:
                        if _city_name_matches(city, str(opt)):
                            return str(opt)
                return str(options[0])
            return listed_cities[0]
        if kind in ("radio", "checkbox_group") and options:
            prefs = [str(p).lower() for p in app_facts.get("preferred_locations", [])]
            picked = [
                str(opt) for opt in options
                if any(pref in str(opt).lower() or str(opt).lower() in pref for pref in prefs)
            ]
            if picked:
                return picked[0] if kind == "radio" else ", ".join(picked)
            relocate = str(app_facts.get("willing_to_relocate", "Yes"))
            for opt in options:
                if relocate.lower() in str(opt).lower():
                    return str(opt)
            if kind == "checkbox_group":
                return ", ".join(options)
            return str(options[0]) if options else relocate
        prefs = app_facts.get("preferred_locations") or [facts.location.split(",")[0]]
        return ", ".join(str(p) for p in prefs)

    if group_id == "total_experience":
        return years

    if group_id == "pan":
        pan = str(app_facts.get("pan", "")).strip()
        return pan or None

    if group_id == "uan":
        uan = str(app_facts.get("uan", "")).strip()
        if kind == "radio":
            opts = field.get("options", [])
            if uan.lower() in ("yes", "no") and opts:
                for opt in opts:
                    if uan.lower() in str(opt).lower():
                        return str(opt)
            return "Yes" if uan else "No"
        return uan or None

    if group_id == "current_employer":
        if facts.experience:
            return str(facts.experience[0].get("company", ""))
        return None

    if group_id == "reason_for_change":
        return str(app_facts.get("reason_for_change", "")).strip() or None

    if group_id == "interview_availability":
        ans = str(app_facts.get("interview_available", "Yes"))
        if kind in ("radio", "checkbox") and options:
            for opt in options:
                if ans.lower() in str(opt).lower():
                    return str(opt)
        if kind == "checkbox":
            return "Yes" if ans.lower() in ("yes", "true", "1") else "No"
        return ans

    if group_id == "reports_to":
        return "0"

    profile_link = profile_link_answer(config, question)
    if profile_link:
        return profile_link

    if re.search(r"\bmiddle name\b", norm):
        return "Skip"

    if re.search(r"\b(date of birth|dob|birth date)\b", norm):
        dob = str(app_facts.get("date_of_birth", "")).strip()
        return dob or None

    if group_id.startswith("skill:"):
        skill = group_id.split(":", 1)[1]
        return skill_years_answer(config, facts, app_facts, skill.replace("_", " "))

    if group_id.startswith("skill_yesno:"):
        skill = group_id.split(":", 1)[1]
        decision = _skill_yesno_decision(question, facts, config, skill, app_facts)
        if decision is None:
            return None
        has = decision
        if kind in ("radio", "checkbox") and options:
            target = "yes" if has else "no"
            for opt in options:
                if target in str(opt).lower():
                    return str(opt)
        if kind == "checkbox":
            return "Yes" if has else "No"
        if has and "how many" in norm:
            return skill_years_answer(config, facts, app_facts, skill.replace("_", " "))
        return "Yes" if has else "No"

    if _expects_short_answer(question):
        return None

    from .application_questions import infer_field_input_type

    if infer_field_input_type(question, field) in (
        "years_numeric",
        "ctc_numeric",
        "number",
        "pincode",
        "date",
        "location",
        "single_choice",
        "yes_no_checkbox",
    ):
        return None

    if "ai" in norm and "backend" in norm:
        text = str(app_facts.get("ai_backend_use_cases", "")).strip()
        if text:
            return text[:500]

    if kind in ("radio", "checkbox"):
        return None

    from .application_questions import is_new_experience_question

    if is_new_experience_question(config, question):
        return None

    # A pure Yes/No question (e.g. "Have you independently created HLD/LLD docs?")
    # must never be answered with a resume blob. Defer so the LLM / manual path can
    # give an actual Yes/No instead of dumping an unrelated paragraph.
    if _looks_like_pure_yes_no(question):
        return None

    # Scale/rating questions with options inline in the label (e.g. "How strong are
    # you in DSA? (Beginner/Intermediate/Advanced)") are constrained choices, not
    # free text — defer so the LLM picks a listed option instead of a resume blob.
    if _inline_choice_options(question):
        return None

    chunks = _retrieve_chunks(question, jd, facts, config)
    if chunks or facts.experience:
        return _compose_text_answer(question, chunks, facts)
    return None
