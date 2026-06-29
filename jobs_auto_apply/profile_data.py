from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ResumeFacts:
    name: str
    headline: str
    email: str
    phone: str
    location: str
    education: str
    technical_skills: dict[str, list[str]]
    experience: list[dict[str, Any]]
    profile_summary: str


def build_resume_context(facts: ResumeFacts) -> str:
    """Structured resume context from profile/resume_facts.yaml."""
    skill_lines = [f"  {category}: {', '.join(items)}" for category, items in facts.technical_skills.items() if items]
    exp_lines: list[str] = []
    for role in facts.experience:
        exp_lines.append(f"- {role.get('title', '')} @ {role.get('company', '')} ({role.get('period', '')})")
        if role.get("location"):
            exp_lines.append(f"  Location: {role['location']}")
        for highlight in role.get("highlights", []):
            exp_lines.append(f"  • {highlight}")

    return f"""CANDIDATE CV (use ONLY these verified facts — never invent employers, skills, or metrics):

Name: {facts.name}
Headline: {facts.headline}
Email: {facts.email} | Phone: {facts.phone}
Location: {facts.location}
Education: {facts.education}

Professional summary:
{facts.profile_summary.strip()}

Technical skills:
{chr(10).join(skill_lines)}

Work experience:
{chr(10).join(exp_lines)}
"""


def load_resume_facts(base_dir: Path) -> ResumeFacts:
    path = base_dir / "profile" / "resume_facts.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Resume facts not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ResumeFacts(
        name=str(raw.get("name", "")),
        headline=str(raw.get("headline", "")),
        email=str(raw.get("email", "")),
        phone=str(raw.get("phone", "")),
        location=str(raw.get("location", "")),
        education=str(raw.get("education", "")),
        technical_skills=dict(raw.get("technical_skills", {})),
        experience=list(raw.get("experience", [])),
        profile_summary=str(raw.get("profile_summary", "")).strip(),
    )
