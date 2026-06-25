"""Shared text normalization for questions and labels."""

from __future__ import annotations

import re


def norm_text(text: str) -> str:
    t = text.lower().strip()
    t = t.replace("&gt;", ">").replace("&lt;", "<")
    t = re.sub(r"[^\w\s/&+.-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()
