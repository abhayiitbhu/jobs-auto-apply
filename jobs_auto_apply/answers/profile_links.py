"""GitHub / LinkedIn profile link detection and answers."""

from __future__ import annotations

import re

from ..config import AppConfig
from .text import norm_text


def is_github_profile_question(question: str) -> bool:
    norm = norm_text(question)
    if "github" not in norm:
        return False
    if re.search(
        r"\bhow many\b|\byears?\b.{0,24}\bexperience\b|\bexperience\b.{0,24}\byears?\b",
        norm,
    ):
        return False
    if re.search(
        r"github.{0,40}(maven|jenkins|sonar|actions|copilot)|ci/?cd.{0,40}github",
        norm,
    ):
        return False
    return bool(
        re.search(
            r"\b(url|link|profile|username|handle|share|insert|provide|paste)\b",
            norm,
        )
    )


def is_linkedin_profile_question(question: str) -> bool:
    norm = norm_text(question)
    if "linkedin" not in norm:
        return False
    if re.search(r"\bhow many\b|\byears?\b", norm):
        return False
    return bool(re.search(r"\b(url|link|profile)\b", norm))


def profile_link_answer(config: AppConfig, question: str) -> str | None:
    if is_linkedin_profile_question(question):
        url = (config.user.linkedin or "").strip()
        return url or None
    if is_github_profile_question(question):
        url = (config.user.github or "").strip()
        return url or None
    return None
