from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from .config import AppConfig
from .profile_data import ResumeFacts, load_resume_facts
from .question_groups import classify_question
from .resume_text import load_resume_text, resume_paragraphs

logger = logging.getLogger("job_apply")

_CONSENT_LABEL = re.compile(
    r"\b(agree|consent|terms|conditions|confirm|acknowledge|accept|declare)\b",
    re.I,
)


def load_application_facts(base_dir: Path) -> dict[str, Any]:
    path = base_dir / "profile" / "application_facts.yaml"
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _norm(text: str) -> str:
    t = text.lower().strip()
    t = t.replace("&gt;", ">").replace("&lt;", "<")
    t = re.sub(r"[^\w\s/&+.-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


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


def _has_skill(facts: ResumeFacts, skill: str, config: AppConfig | None = None) -> bool:
    skill_l = skill.lower()
    blob = " ".join(facts.all_skills_flat()).lower()
    blob += " " + facts.profile_summary.lower()
    for role in facts.experience:
        blob += " " + " ".join(role.get("highlights", [])).lower()
    if config is not None:
        blob += " " + load_resume_text(config).lower()
    aliases = {
        "java": ("java", "spring", "j2ee", "microservice"),
        "python": ("python", "fastapi", "flask", "django"),
        "aws": ("aws", "ec2", "lambda", "s3", "cloudwatch"),
        "react": ("react",),
        "dotnet": (".net", "asp", "c#"),
        "ai": ("openai", "machine learning", "ml ", " llm"),
        "postgresql": ("postgres", "postgresql"),
    }
    for key, words in aliases.items():
        if key in skill_l or skill_l in key:
            return any(w in blob for w in words)
    return skill_l in blob


def _expects_short_answer(question: str) -> bool:
    norm = _norm(question)
    return bool(
        re.search(
            r"how many|years|ctc|salary|usd|monthly|linkedin|url|when would|"
            r"available to start|rate your|comfortable|based in india|cet working|"
            r"associated with|previously employed|military spouse|identify as|"
            r"\boffers?\b|date of birth|dob|birth date",
            norm,
        )
    )


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
    current = (
        f"{comp.current_ctc_lpa:.0f} LPA "
        f"({comp.current_fixed_lpa:.0f}L fixed + {comp.current_variable_lpa:.0f}L variable + "
        f"{comp.current_esops_lpa:.0f}L ESOPs)"
    )
    expected = f"{comp.expected_ctc_lpa:.0f} LPA"
    if "expected" in norm and "current" not in norm:
        return expected
    if "current" in norm and "expected" not in norm:
        return current
    if "salary expectation" in norm:
        return expected
    return f"Current: {current}. Expected: {expected}."


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
        if _has_skill(facts, opt_s, config):
            matched.append(opt_s)
    return ", ".join(matched) if matched else None


def _yes_no_option(options: list[Any]) -> bool:
    opts = {str(o).strip().lower() for o in options if str(o).strip()}
    return opts.issubset({"yes", "no"}) and len(opts) >= 2


def _yes_no_radio_answer(question: str, app_facts: dict[str, Any], norm: str) -> str | None:
    if re.search(r"join.*(immediately|within\s*15)|within\s*15\s*days|15\s*days", norm):
        days = app_facts.get("notice_period_days")
        if days is not None:
            return "Yes" if int(days) <= 15 else "No"
    if re.search(r"comfortable|willing|work from office|\bwfo\b|relocat|gurgaon|gurugram|bangalore|bengaluru|hyderabad|mumbai|pune|delhi|ncr", norm):
        return str(app_facts.get("willing_to_relocate", "Yes"))
    if re.search(r"pf|provident|insurance|employment package", norm):
        return "Yes"
    if re.search(r"\binterview|available\b", norm):
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
    app_facts = load_application_facts(config.base_dir)
    group_id = classify_question(question)
    norm = _norm(question)
    years = str(config.profile.years_experience)
    kind = str(field.get("kind", "text"))
    options = [str(o) for o in field.get("options", []) if str(o).strip()]

    if re.search(r"\bmilitary spouse\b", norm):
        if kind in ("radio", "checkbox") and options:
            picked = _pick_negative_option(options)
            if picked:
                return picked
        return "No"

    if re.search(
        r"\b(associated with|previously employed|currently employed at|employee of|"
        r"employed by|worked (?:at|for)|received an offer from|previously or are currently)\b",
        norm,
    ):
        company_hint = ""
        for pattern in (
            r"associated with\s+(.+?)\??$",
            r"employed (?:at|by)\s+(.+?)\??$",
            r"worked (?:at|for)\s+(.+?)\??$",
            r"received an offer from\s+(.+?)\??$",
        ):
            match = re.search(pattern, norm, re.I)
            if match:
                company_hint = match.group(1).strip()
                break
        if not company_hint:
            for token in re.findall(r"[a-z0-9]+", norm):
                if len(token) >= 4 and token not in {
                    "have", "been", "previously", "currently", "associated", "with", "employed",
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
            yn = _yes_no_radio_answer(question, app_facts, norm)
            if yn:
                return yn
            days = app_facts.get("notice_period_days")
            if days is not None:
                return "Yes" if int(days) <= 15 else "No"
        return None

    if kind == "checkbox":
        if _CONSENT_LABEL.search(question):
            return "Yes"
        if group_id.startswith("skill_yesno:"):
            skill = group_id.split(":", 1)[1]
            return "Yes" if _has_skill(facts, skill, config) else "No"
        return "Yes"

    if kind == "radio" and (_yes_no_option(options) or not options):
        yn = _yes_no_radio_answer(question, app_facts, norm)
        if yn and yn.lower() in ("yes", "no"):
            return yn
        if group_id.startswith("skill_yesno:"):
            skill = group_id.split(":", 1)[1]
            return "Yes" if _has_skill(facts, skill, config) else "No"
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
            if _has_skill(facts, skill, config):
                return ", ".join(
                    opt for opt in options
                    if _has_skill(facts, str(opt), config) or skill in str(opt).lower()
                ) or options[0]
            return "No"
        if len(options) == 1:
            return options[0]
        return "all"

    if group_id == "compensation":
        return _current_ctc_answer(config, question)

    if group_id == "notice_period":
        days = app_facts.get("notice_period_days")
        if days is None:
            return None
        if app_facts.get("serving_notice") and app_facts.get("last_working_day"):
            return f"Serving notice period. Last working day: {app_facts['last_working_day']}."
        if "days" in norm:
            return str(days)
        return f"{days} days"

    if group_id == "current_location":
        city = facts.location.split(",")[0].strip()
        return city or facts.location

    if group_id == "preferred_location":
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

    if "linkedin" in norm and "url" in norm:
        url = (config.user.linkedin or "").strip()
        return url or None

    if re.search(r"\bmiddle name\b", norm):
        return "Skip"

    if re.search(r"\b(date of birth|dob|birth date)\b", norm):
        dob = str(app_facts.get("date_of_birth", "")).strip()
        return dob or None

    if _expects_short_answer(question):
        return None

    if group_id.startswith("skill_yesno:"):
        skill = group_id.split(":", 1)[1]
        has = _has_skill(facts, skill, config)
        if kind in ("radio", "checkbox") and options:
            target = "yes" if has else "no"
            for opt in options:
                if target in str(opt).lower():
                    return str(opt)
        if kind == "checkbox":
            return "Yes" if has else "No"
        if has and "how many" in norm:
            return years
        return "Yes" if has else "No"

    if group_id.startswith("skill:"):
        return years

    if "ai" in norm and "backend" in norm:
        text = str(app_facts.get("ai_backend_use_cases", "")).strip()
        if text:
            return text[:500]

    if kind in ("radio", "checkbox"):
        return None

    chunks = _retrieve_chunks(question, jd, facts, config)
    if chunks or facts.experience:
        return _compose_text_answer(question, chunks, facts)
    return None
