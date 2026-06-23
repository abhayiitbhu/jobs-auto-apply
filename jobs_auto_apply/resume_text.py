from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import AppConfig

logger = logging.getLogger("job_apply")

_CACHE_NAME = "resume_text.cache.txt"


def _cache_path(base_dir: Path) -> Path:
    return base_dir / "data" / _CACHE_NAME


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        logger.warning("pypdf not installed; resume PDF text unavailable: %s", exc)
        return ""

    if not path.exists():
        logger.warning("Resume PDF not found: %s", path)
        return ""

    try:
        reader = PdfReader(str(path))
        parts: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts).strip()
    except Exception as exc:
        logger.warning("Failed to extract resume PDF text from %s: %s", path, exc)
        return ""


def load_resume_text(config: AppConfig, *, force_refresh: bool = False) -> str:
    """Extract and cache plain text from config.resume_path PDF."""
    pdf_path = config.resume_path
    cache = _cache_path(config.base_dir)

    if (
        not force_refresh
        and cache.exists()
        and pdf_path.exists()
        and cache.stat().st_mtime >= pdf_path.stat().st_mtime
    ):
        return cache.read_text(encoding="utf-8").strip()

    text = _extract_pdf_text(pdf_path)
    if text:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
        logger.info("Cached resume text (%d chars) from %s", len(text), pdf_path.name)
    return text


def _paragraphs(text: str) -> list[str]:
    raw = re.split(r"\n{2,}|\s{2,}", text)
    out: list[str] = []
    for block in raw:
        p = re.sub(r"\s+", " ", block).strip()
        if len(p) >= 20:
            out.append(p)
    return out or ([text.strip()] if text.strip() else [])


def _tokens(text: str) -> set[str]:
    stop = {
        "the", "and", "for", "with", "from", "that", "this", "your", "have", "years",
        "experience", "using", "worked", "role", "team", "project",
    }
    return {w for w in re.findall(r"[a-z0-9+#./-]+", text.lower()) if len(w) > 2 and w not in stop}


def resume_paragraphs(config: AppConfig) -> list[str]:
    return _paragraphs(load_resume_text(config))


def relevant_resume_excerpt(
    config: AppConfig,
    question: str = "",
    *,
    max_chars: int = 3500,
) -> str:
    """Return resume text most relevant to a question (or leading excerpt if no question)."""
    full = load_resume_text(config)
    if not full:
        return ""

    if not question.strip():
        return full[:max_chars]

    query = _tokens(question)
    if not query:
        return full[:max_chars]

    scored: list[tuple[float, str]] = []
    for para in _paragraphs(full):
        pt = _tokens(para)
        if not pt:
            continue
        overlap = len(query & pt) / max(len(query), 1)
        scored.append((overlap, para))
    scored.sort(key=lambda x: x[0], reverse=True)

    picked: list[str] = []
    total = 0
    for score, para in scored:
        if score <= 0 and picked:
            break
        if total + len(para) > max_chars:
            remain = max_chars - total
            if remain > 120:
                picked.append(para[:remain].rsplit(" ", 1)[0] + "...")
            break
        picked.append(para)
        total += len(para)
        if total >= max_chars:
            break

    if not picked:
        return full[:max_chars]
    return "\n\n".join(picked)
