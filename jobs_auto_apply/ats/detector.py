from __future__ import annotations

import re
from urllib.parse import urlparse

ATS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards|board)\.greenhouse\.io", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co|lever\.co", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com", re.I)),
    ("workday", re.compile(r"(?:\w+\.)?myworkdayjobs\.com|workday\.com/.*/job/", re.I)),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com|jobs\.smartrecruiters\.com", re.I)),
    ("icims", re.compile(r"icims\.com", re.I)),
    ("bamboohr", re.compile(r"bamboohr\.com", re.I)),
    ("teamtailor", re.compile(r"teamtailor\.com", re.I)),
    ("jobvite", re.compile(r"jobvite\.com", re.I)),
    ("recruitee", re.compile(r"recruitee\.com", re.I)),
]


def detect_ats(url: str) -> str:
    for name, pattern in ATS_PATTERNS:
        if pattern.search(url):
            return name
    host = urlparse(url).netloc.lower()
    if host and "careers" in host:
        return "careers_portal"
    return "generic"
