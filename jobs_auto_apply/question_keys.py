from __future__ import annotations

import hashlib
import re


def question_key(text: str) -> str:
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()[:16]
