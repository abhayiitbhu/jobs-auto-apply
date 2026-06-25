"""Detect skill-years and new/unconfigured experience questions."""

from __future__ import annotations

import re

from ..config import AppConfig
from ..profile.skills import load_skill_context, skill_experience_configured


def is_skill_years_question(question: str) -> bool:
    from ..question_groups import classify_question

    group_id = classify_question(question)
    if group_id.startswith("skill:"):
        return True
    norm = question.lower()
    return bool(
        re.search(r"^experience in\b|\bai domains?\b", norm)
        or (
            re.search(r"\bhow (many|much)\b", norm)
            and re.search(r"\bexperience\b", norm)
            and re.search(r"\b(in|with|on|using|as)\s+[a-z0-9]", norm)
        )
        or (
            re.search(r"\bhow many\b", norm)
            and re.search(r"\byears?\b", norm)
            and re.search(r"\bexperience\b", norm)
            and re.search(r"\b(in|with|on|using|as)\s+[a-z0-9]", norm)
        )
    )


def is_new_experience_question(config: AppConfig, question: str) -> bool:
    """
    Experience for skills not in application_facts.skill_years / core_skills —
    require high-confidence LLM or queue.
    """
    from ..question_groups import classify_question

    q = question.strip()
    if not q:
        return False
    gid = classify_question(q)
    facts, app_facts = load_skill_context(config)
    norm = q.lower()

    if gid.startswith("skill:"):
        if not is_skill_years_question(q):
            return False
        skill = gid.split(":", 1)[1].replace("_", " ")
        return not skill_experience_configured(config, facts, app_facts, skill)

    if gid.startswith("skill_yesno:"):
        skill = gid.split(":", 1)[1].replace("_", " ")
        return not skill_experience_configured(config, facts, app_facts, skill)

    if is_skill_years_question(q):
        return True
    if re.search(r"\b(how much|how many)\b", norm) and re.search(r"\bexperience\b", norm):
        return True
    if re.search(r"\bdo you have experience\b", norm) and not re.search(
        r"\bhow many\b", norm
    ):
        return True
    if re.search(r"^experience in\b", norm):
        return True
    return False
