from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .config import AppConfig
from .memory import load_memory
from .profile_data import build_resume_context, load_resume_facts
from .question_groups import classify_question
from .rag_answers import load_application_facts

logger = logging.getLogger("job_apply")

_QUOTE_WRAP = re.compile(r'^["\'](.+)["\']$', re.S)
# Strip emoji / pictographic characters that occasionally leak into letters.
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\U00002190-\U000021ff\U00002b00-\U00002bff\ufe0f]"
)
# Leading list markers (bullets, dashes, asterisks, numbered) at line start.
_LIST_MARKER_RE = re.compile(r"^\s*(?:[•\-\*\u2022\u25cf\u25aa\u2023\u2043\u2219]|\d+[.)])\s+")


def _sanitize_cover_letter(text: str) -> str:
    """Force LLM cover-letter output into clean prose.

    The reference letter and some models produce emoji headers, bullet lists, and
    resume-style section labels. Strip those so the result reads like a letter.
    """
    text = _EMOJI_RE.sub("", text)
    out_lines: list[str] = []
    for raw_line in text.splitlines():
        line = _LIST_MARKER_RE.sub("", raw_line).strip()
        out_lines.append(line)
    text = "\n".join(out_lines)
    # Collapse 3+ blank lines down to a single blank line between paragraphs.
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Drop stray spaces left by removed emojis.
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# Floor on how many top vector matches are surfaced to the LLM for answer
# generation. Even when rag_top_k is configured lower, the LLM should see at
# least this many similar prior Q/A pairs so it has enough context to adapt.
_MIN_RAG_FOR_LLM = 10
_FAISS_STORE_NAME = "user_memory_qa"
_FAISS_CACHE: dict[str, tuple[float, Any]] = {}
_OLLAMA_LOCK = threading.Lock()  # legacy single-flight (kept for any direct users)
_OLLAMA_GATES: dict[int, threading.BoundedSemaphore] = {}
_OLLAMA_GATES_LOCK = threading.Lock()
_PROFILE_CTX_CACHE: dict[str, tuple[tuple[float, ...], str]] = {}


def _ollama_gate(config: AppConfig) -> threading.BoundedSemaphore:
    """Bounded concurrency for local Ollama calls.

    Weights are loaded once and shared across concurrent requests, so allowing a
    couple of in-flight calls overlaps inference and noticeably improves
    throughput when several apply-workers hit the LLM at once. Sized from
    ``llm.max_concurrency``; drop it to 1 if a long run shows RAM pressure.
    """
    n = max(1, int(getattr(config.llm, "max_concurrency", 1) or 1))
    with _OLLAMA_GATES_LOCK:
        gate = _OLLAMA_GATES.get(n)
        if gate is None:
            gate = threading.BoundedSemaphore(n)
            _OLLAMA_GATES[n] = gate
        return gate


def unload_ollama_model(config: AppConfig, model_name: str) -> bool:
    """Remove a model from Ollama memory (no-op if not loaded)."""
    name = (model_name or "").strip()
    if not name:
        return False
    base_url = (config.llm.base_url or "http://127.0.0.1:11434").rstrip("/")
    for endpoint, payload in (
        ("/api/stop", {"model": name}),
        ("/api/generate", {"model": name, "keep_alive": 0}),
    ):
        try:
            req = urllib.request.Request(
                f"{base_url}{endpoint}",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            logger.info("Unloaded Ollama model: %s", name)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            logger.debug("Ollama unload %s via %s: %s", name, endpoint, exc)
        except Exception as exc:
            logger.debug("Ollama unload %s via %s: %s", name, endpoint, exc)
    return False


def ensure_verifier_unloaded(config: AppConfig) -> None:
    """When verifier is disabled, free VRAM by unloading the verifier model."""
    llm = config.llm
    if llm.verifier_enabled or not llm.verifier_model:
        return
    unload_ollama_model(config, llm.verifier_model)


@dataclass
class SimilarAnswer:
    question: str
    answer: str
    score: float  # higher = more similar


@dataclass
class LLMDecision:
    answer: str
    confidence: float
    canonical: str | None = None
    # True only when the separate verifier model actually inspected this answer
    # (high-risk field + verifier enabled) and approved it — an independent
    # corroboration signal, distinct from the model's self-reported confidence.
    verified: bool = False


def _strip_llm_text(text: str) -> str:
    answer = (text or "").strip()
    m = _QUOTE_WRAP.match(answer)
    if m:
        answer = m.group(1).strip()
    answer = re.sub(r"^(answer|response)\s*:\s*", "", answer, flags=re.I).strip()
    return answer


def _field_instructions(field: dict[str, Any]) -> str:
    from .application_questions import enrich_field_for_llm, is_numeric_ctc_question

    field = enrich_field_for_llm(field)
    kind = str(field.get("kind", "text"))
    input_type = str(field.get("input_type", kind))
    label = str(field.get("label", ""))
    placeholder = str(field.get("placeholder", "")).strip()
    platform = str(field.get("platform", "")).strip()
    options = [str(o).strip() for o in field.get("options", []) if str(o).strip()]

    lines: list[str] = [
        "=== FORM FIELD (match answer format to this) ===",
        f"Control kind: {kind}",
        f"Expected value type: {input_type}",
    ]
    if platform:
        lines.append(f"Platform: {platform}")
    if placeholder:
        lines.append(f"Input placeholder: {placeholder}")
    if field.get("hasVisibleInput"):
        lines.append("UI: free-text input box (not chips/radio)")
    if field.get("hasDobInput"):
        lines.append("UI: date-of-birth triplet (DD/MM/YYYY)")

    if input_type == "pincode":
        lines.append("Reply with postal pincode digits only (e.g. 560001). No Yes/No.")
    elif input_type == "location":
        lines.append(
            "Reply with a city or location name only (e.g. Bengaluru). "
            "Never reply with a number, Yes/No, or years of experience."
        )
    elif input_type == "ctc_numeric" or is_numeric_ctc_question(label):
        if re.search(r"\bexpected\b", label, re.I) and not re.search(r"\bcurrent\b", label, re.I):
            lines.append(
                "Reply with EXPECTED CTC only — a single number in lakhs (e.g. 45). "
                "No currency, breakdown, or current CTC."
            )
        elif re.search(r"\bcurrent\b", label, re.I):
            lines.append(
                "Reply with CURRENT CTC only — a single number in lakhs (e.g. 38). "
                "No currency, breakdown, or expected CTC."
            )
        else:
            lines.append(
                "Reply with CTC as a single number in lakhs only (e.g. 38). No LPA text, breakdown, or sentences."
            )
    elif input_type == "years_numeric":
        platform = str(field.get("platform", "")).lower()
        if platform == "naukri":
            lines.append(
                "Reply with years as a single integer only (e.g. 4 or 0). "
                "No 'years' suffix, units, or sentences. "
                "Use 0 when the candidate has no experience in that exact skill "
                "(see application_facts.skill_years if set)."
            )
        else:
            lines.append(
                "Reply with years of experience as a number (e.g. 5 or 5 years). "
                "Use 0 only if the candidate truly has no experience in that skill. "
                "Do not include salary, CTC, or unrelated details."
            )
    elif input_type == "date":
        lines.append("Reply with date as DD/MM/YYYY (e.g. 15/08/1995).")
    elif input_type == "number":
        lines.append("Reply with a number only — no units or explanation unless the question asks.")
    elif input_type == "single_choice":
        if options:
            lines.append(f"Options (reply with exactly one label): {', '.join(options)}")
            lines.append("Reply with exactly one option label from the list — no extra text.")
            if any(re.search(r"<\s*\d+|\d+\s*[-–]\s*\d+|\d+\s*\+", o) for o in options):
                lines.append(
                    "For year-range chips, canonical should be the candidate's actual years "
                    "(e.g. '5') from profile/resume — not the range label."
                )
        else:
            lines.append("Reply with exactly Yes or No.")
    elif input_type == "multi_choice":
        if options:
            lines.append(f"Options: {', '.join(options)}")
            lines.append("Reply with comma-separated option labels from the list.")
    elif input_type == "yes_no_checkbox" or (kind in ("radio", "checkbox") and not options):
        lines.append("Reply with exactly Yes or No.")
    elif kind in ("input", "textarea", "text", "short_text"):
        lines.append("Reply with a short, direct answer. Use digits only when the field expects a number.")

    if options and input_type not in ("single_choice", "multi_choice"):
        lines.append(f"Available options: {', '.join(options)}")

    if re.search(
        r"\b(associated with|previously employed|employed by|worked (?:at|for)|"
        r"received an offer from|military spouse)\b",
        label,
        re.I,
    ):
        lines.append(
            "Past-employer check: answer Yes ONLY if the company is in the work experience list; "
            "otherwise No. Reply with exactly Yes or No."
        )

    lines.append("=== END FORM FIELD ===")
    return "\n".join(lines)


def _profile_source_mtimes(config: AppConfig) -> tuple[float, ...]:
    base = config.base_dir
    paths = (
        base / "profile" / "resume_facts.yaml",
        base / "profile" / "application_facts.yaml",
    )
    return tuple(p.stat().st_mtime if p.exists() else 0.0 for p in paths)


def clear_profile_context_cache() -> None:
    """Drop cached profile text (call at start of each apply run)."""
    _PROFILE_CTX_CACHE.clear()


def _build_application_context(config: AppConfig) -> str:
    """Verified candidate profile from resume_facts.yaml + application_facts + compensation."""
    cache_key = str(config.base_dir.resolve())
    mtimes = _profile_source_mtimes(config)
    cached = _PROFILE_CTX_CACHE.get(cache_key)
    if cached and cached[0] == mtimes:
        return cached[1]

    facts = load_resume_facts(config.base_dir)
    app_facts = load_application_facts(config)
    comp = config.compensation

    lines = [
        build_resume_context(facts),
        "",
        "Application facts:",
        f"- Years of experience (profile): {config.profile.years_experience}",
        f"- Current CTC: {comp.current_ctc_lpa:g} LPA "
        f"({comp.current_fixed_lpa:g}L fixed + {comp.current_variable_lpa:g}L variable + "
        f"{comp.current_esops_lpa:g}L ESOPs)",
        f"- Expected CTC: {comp.expected_ctc_lpa:g} LPA",
        f"- LinkedIn: {config.user.linkedin}",
    ]
    if config.user.github:
        lines.append(f"- GitHub: {config.user.github}")

    for key, value in app_facts.items():
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            lines.append(f"- {key}: {', '.join(str(v) for v in value)}")
        else:
            lines.append(f"- {key}: {value}")

    text = "\n".join(lines)
    _PROFILE_CTX_CACHE[cache_key] = (mtimes, text)
    return text


def _memory_entries(config: AppConfig) -> list[tuple[str, str]]:
    data = load_memory(config.base_dir)
    raw_entries = data.get("question_answers", {})
    if not isinstance(raw_entries, dict):
        return []
    out: list[tuple[str, str]] = []
    for raw in raw_entries.values():
        if not isinstance(raw, dict):
            continue
        q = str(raw.get("question", "")).strip()
        a = str(raw.get("answer", "")).strip()
        if q and a:
            out.append((q, a))
    return out


def _faiss_index_path(config: AppConfig) -> Path:
    return config.base_dir / config.llm.faiss_index_dir / _FAISS_STORE_NAME


def _memory_mtime(config: AppConfig) -> float:
    p = config.user_memory_path
    return p.stat().st_mtime if p.exists() else 0.0


def _get_faiss_store(config: AppConfig):
    if not config.llm.use_faiss_memory:
        return None

    try:
        from langchain_community.vectorstores import FAISS
    except Exception as exc:
        logger.debug("FAISS dependencies unavailable, falling back: %s", exc)
        return None

    from .embeddings import get_embeddings

    idx_path = _faiss_index_path(config)
    cache_key = str(idx_path.resolve())
    mtime = _memory_mtime(config)
    cached = _FAISS_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]

    embeddings = get_embeddings(config.llm.embeddings_model)
    if embeddings is None:
        _FAISS_CACHE[cache_key] = (mtime, None)
        return None

    entries = _memory_entries(config)
    if not entries:
        return None

    texts = [f"Q: {q}\nA: {a}" for q, a in entries]
    metadatas = [{"q": q, "a": a, "gid": classify_question(q)} for q, a in entries]

    store = None
    idx_dir = idx_path.parent
    idx_dir.mkdir(parents=True, exist_ok=True)
    if (idx_path / "index.faiss").exists():
        try:
            loaded = FAISS.load_local(
                str(idx_path),
                embeddings,
                allow_dangerous_deserialization=True,
            )
            if loaded.index.ntotal == len(texts):
                store = loaded
        except Exception as exc:
            logger.debug("Failed loading FAISS index, rebuilding: %s", exc)

    if store is None:
        try:
            store = FAISS.from_texts(texts, embedding=embeddings, metadatas=metadatas)
        except Exception as exc:
            logger.warning("FAISS index build failed, falling back: %s", exc)
            _FAISS_CACHE[cache_key] = (mtime, None)
            return None
        try:
            store.save_local(str(idx_path))
        except Exception as exc:
            logger.debug("Failed saving FAISS index locally: %s", exc)

    _FAISS_CACHE[cache_key] = (mtime, store)
    return store


def _distance_to_similarity(distance: float) -> float:
    return 1.0 / (1.0 + max(float(distance), 0.0))


def retrieve_similar_answers(
    config: AppConfig,
    question: str,
    *,
    k: int | None = None,
) -> list[SimilarAnswer]:
    """Top-k prior Q/A pairs from user_memory via FAISS similarity_search_with_score."""
    limit = k if k is not None else max(config.llm.rag_top_k, _MIN_RAG_FOR_LLM)
    limit = max(1, limit)

    store = _get_faiss_store(config)
    if store is not None:
        try:
            results = store.similarity_search_with_score(question, k=limit)
            picked: list[SimilarAnswer] = []
            seen: set[tuple[str, str]] = set()
            for doc, distance in results:
                meta = doc.metadata or {}
                q = str(meta.get("q", "")).strip()
                a = str(meta.get("a", "")).strip()
                if not q or not a:
                    continue
                key = (q.lower(), a.lower())
                if key in seen:
                    continue
                seen.add(key)
                picked.append(
                    SimilarAnswer(
                        question=q,
                        answer=a,
                        score=_composite_similarity_score(question, q, distance),
                    )
                )
            if picked:
                picked.sort(key=lambda x: x.score, reverse=True)
                return picked[:limit]
        except Exception as exc:
            logger.debug("FAISS similarity_search failed, fallback to lexical: %s", exc)

    lexical = _lexical_similar_answers(config, question, limit=limit)
    lexical.sort(key=lambda x: x.score, reverse=True)
    return lexical[:limit]


def _token_set(text: str) -> set[str]:
    q_norm = re.sub(r"\s+", " ", text.strip().lower())
    return {t for t in re.findall(r"[a-z0-9+#./-]+", q_norm) if len(t) > 2}


def _composite_similarity_score(question: str, matched_question: str, distance: float) -> float:
    """Blend embedding distance, question group, and token overlap (group weighted for accuracy)."""
    cosine_sim = _distance_to_similarity(distance)
    current_group = classify_question(question)
    matched_group = classify_question(matched_question)
    group_match = 1.0 if current_group == matched_group else 0.0
    query_tokens = _token_set(question)
    match_tokens = _token_set(matched_question)
    token_overlap = len(query_tokens & match_tokens) / max(len(query_tokens), 1) if query_tokens else 0.0
    return 0.50 * cosine_sim + 0.40 * group_match + 0.10 * token_overlap


def retrieve_best_similar_answer(
    config: AppConfig,
    question: str,
    *,
    require_same_group: bool = True,
) -> SimilarAnswer | None:
    """Best composite-scored prior Q/A for vector top-1 auto-answer."""
    candidates = retrieve_similar_answers(config, question, k=max(config.llm.rag_top_k, _MIN_RAG_FOR_LLM))
    if not candidates:
        return None

    current_group = classify_question(question)
    best: SimilarAnswer | None = None
    for item in candidates:
        if require_same_group and classify_question(item.question) != current_group:
            continue
        if best is None or item.score > best.score:
            best = item
    return best


def _lexical_similar_answers(
    config: AppConfig,
    question: str,
    *,
    limit: int,
) -> list[SimilarAnswer]:
    data = load_memory(config.base_dir)
    entries = data.get("question_answers", {})
    if not isinstance(entries, dict):
        return []

    current_group = classify_question(question)
    q_norm = re.sub(r"\s+", " ", question.strip().lower())
    query_tokens = {t for t in re.findall(r"[a-z0-9+#./-]+", q_norm) if len(t) > 2}

    scored: list[tuple[float, str, str]] = []
    for raw in entries.values():
        if not isinstance(raw, dict):
            continue
        q = str(raw.get("question", "")).strip()
        a = str(raw.get("answer", "")).strip()
        if not q or not a:
            continue

        score = 0.0
        if classify_question(q) == current_group:
            score += 1.0
        q2 = q.lower()
        if q_norm in q2 or q2 in q_norm:
            score += 0.8
        tokens2 = {t for t in re.findall(r"[a-z0-9+#./-]+", q2) if len(t) > 2}
        if query_tokens and tokens2:
            score += 0.4 * (len(query_tokens & tokens2) / len(query_tokens))
        if score <= 0:
            continue
        scored.append((score, q, a))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [SimilarAnswer(question=q, answer=a, score=score) for score, q, a in scored[:limit]]


def _format_free_tier_hints(ctx: Any) -> str:
    """Format deterministic pre-LLM hints so the model can answer in one shot."""
    lines: list[str] = []
    if ctx.config_hint and str(ctx.config_hint).strip():
        lines.append(f"Config/rules answer: {ctx.config_hint.strip()}")
    if ctx.rag_raw and str(ctx.rag_raw).strip():
        raw = str(ctx.rag_raw).strip()
        fill = str(ctx.rag_fill).strip() if ctx.rag_fill else ""
        if fill and fill != raw:
            lines.append(f"Rule RAG answer (field-formatted): {fill}")
        lines.append(f"Rule RAG answer: {raw}")
    elif ctx.rag_fill and str(ctx.rag_fill).strip():
        lines.append(f"Rule RAG answer: {ctx.rag_fill.strip()}")
    if ctx.vector_best is not None:
        v = ctx.vector_best
        lines.append(f"Vector match (score={v.score:.3f}, same question group): Q: {v.question} → A: {v.answer}")
    if not lines:
        return ""
    return (
        "\n\n=== PRE-COMPUTED HINTS (from rules/config/memory — use when correct for this field) ===\n"
        + "\n".join(lines)
        + "\nPrefer the highest-confidence hint that matches the field format and profile."
    )


def _format_similar_answers(answers: list[SimilarAnswer]) -> str:
    if not answers:
        return ""
    lines = [f"Top {len(answers)} similar prior answers (RAG similarity search):"]
    for i, item in enumerate(answers, 1):
        lines.append(f"{i}. (similarity={item.score:.3f}) Q: {item.question}")
        lines.append(f"   A: {item.answer}")
    return "\n".join(lines)


def _memory_examples(
    config: AppConfig,
    question: str,
    *,
    similar_answers: list[SimilarAnswer] | None = None,
) -> str:
    """Format similarity-ranked prior Q/A pairs for the LLM prompt."""
    answers = similar_answers if similar_answers is not None else retrieve_similar_answers(config, question)
    return _format_similar_answers(answers)


def _normalize_llm_answer(answer: str, field: dict[str, Any]) -> str:
    from .answers.chips import _normalize_to_option
    from .answers.compensation import resolve_ctc_numeric_answer
    from .answers.fields import enrich_field_for_llm

    field = enrich_field_for_llm(field)
    text = _strip_llm_text(answer)
    if not text:
        return ""

    kind = str(field.get("kind", "text"))
    input_type = str(field.get("input_type", kind))
    label = str(field.get("label", ""))
    options = [str(o).strip() for o in field.get("options", []) if str(o).strip()]

    if options:
        picked = _normalize_to_option(text, options)
        if picked:
            return picked
        text_l = text.lower()
        for opt in options:
            if opt.lower() in text_l:
                return opt

    if kind == "checkbox":
        if re.search(r"\bno\b", text, re.I):
            return "No"
        if re.search(r"\byes\b", text, re.I):
            return "Yes"

    if kind in ("radio", "checkbox") and not options:
        if re.search(r"\bno\b", text, re.I) and not re.search(r"\byes\b", text, re.I):
            return "No"
        if re.search(r"\byes\b", text, re.I):
            return "Yes"

    if input_type == "ctc_numeric":
        resolved = resolve_ctc_numeric_answer(label, text, None)
        if resolved and re.fullmatch(r"\d+(?:\.\d+)?", resolved):
            return resolved

    if input_type in ("years_numeric", "number"):
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if match:
            num = match.group(1)
            platform = str(field.get("platform", "")).lower()
            if input_type == "years_numeric" and platform == "naukri":
                return str(int(float(num))) if float(num) == int(float(num)) else num
            if input_type == "years_numeric" and re.search(r"\byears?\b", label, re.I):
                return f"{num} years" if not re.search(r"\byears?\b", text, re.I) else text
            return num

    if input_type == "pincode":
        match = re.search(r"(\d{4,8})", text)
        if match:
            return match.group(1)

    if input_type == "location":
        if re.fullmatch(r"\d+(?:\.\d+)?", text.strip()):
            return ""
        if text.strip().lower() in ("yes", "no"):
            return ""

    if len(text) > 500 and kind in ("input", "text", "textarea"):
        text = text[:497].rsplit(" ", 1)[0] + "..."

    return text


def _parse_llm_json(raw: str) -> tuple[str, float, str | None] | None:
    text = (raw or "").strip()
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    payload = match.group(0) if match else text
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    answer = _strip_llm_text(str(obj.get("answer", "")))
    canonical = _strip_llm_text(str(obj.get("canonical", ""))) or None
    try:
        confidence = float(obj.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    if not answer:
        return None
    return answer, confidence, canonical


def _chat_model(config: AppConfig, *, num_predict: int | None = None) -> Any | None:
    llm = config.llm
    if not llm.enabled:
        return None
    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        logger.warning("langchain-ollama is not installed — pip install langchain-ollama")
        return None
    base_url = (llm.base_url or "http://127.0.0.1:11434").rstrip("/")
    return ChatOllama(
        base_url=base_url,
        model=llm.model or "job-answers",
        temperature=llm.temperature,
        num_predict=num_predict if num_predict is not None else llm.max_tokens,
        keep_alive=llm.keep_alive or "30m",
    )


def _verifier_model(config: AppConfig) -> Any | None:
    llm = config.llm
    if not llm.verifier_enabled or not llm.verifier_model:
        return None
    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        return None
    base_url = (llm.base_url or "http://127.0.0.1:11434").rstrip("/")
    return ChatOllama(
        base_url=base_url,
        model=llm.verifier_model,
        temperature=0.0,
        num_predict=64,
        keep_alive=llm.keep_alive or "30m",
    )


def verify_llm_answer_detailed(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    fill_answer: str,
    profile_excerpt: str = "",
    similar_answers: list[SimilarAnswer] | None = None,
    rag_hint: str | None = None,
) -> tuple[bool, bool]:
    """Independent verifier model. Returns ``(ok, actually_verified)``.

    Runs for EVERY answer (not just high-risk) so it can serve as the required
    second source backing an LLM answer. It checks the proposed answer against the
    candidate profile AND the databank (past answers + rule/RAG hints), so a value
    is approved when it is supported by either.

    ``actually_verified`` is True only when a verifier model genuinely ran and
    approved — that is the corroboration signal. It is False when the verifier is
    disabled/unavailable, which must NOT be treated as backing.
    """
    from .application_questions import enrich_field_for_llm

    field = enrich_field_for_llm(field)

    model = _verifier_model(config)
    if not model:
        return True, False

    databank_lines: list[str] = []
    if rag_hint and str(rag_hint).strip():
        databank_lines.append(f"Rules/RAG suggestion: {str(rag_hint).strip()}")
    for item in (similar_answers or [])[:3]:
        ans = str(getattr(item, "answer", "") or "").strip()
        ques = str(getattr(item, "question", "") or "").strip()
        if ans:
            databank_lines.append(f"Past answer — Q: {ques[:80]} -> A: {ans[:80]}")
    databank_block = (
        "\n\n=== CANDIDATE DATABANK (past answers + rules) ===\n" + "\n".join(databank_lines) if databank_lines else ""
    )

    system = SystemMessage(
        content=(
            "You verify job application answers against the candidate profile AND "
            "databank. "
            'Return strict JSON only: {"ok": true} or {"ok": false}. '
            "ok=true only when the answer is supported by the profile or the "
            "databank (past answers / rules) and matches the field format. "
            "ok=false if it contradicts them or is unsupported invention."
        )
    )
    human = HumanMessage(
        content=(
            f"Profile:\n{profile_excerpt[:2000]}\n"
            f"{databank_block}\n\n"
            f"Field: {field.get('kind', 'text')} / {field.get('input_type', '')}\n"
            f"Options: {', '.join(str(o) for o in field.get('options', []) if o)}\n"
            f"Question: {question}\n"
            f"Proposed answer: {fill_answer}"
        )
    )
    try:
        with _ollama_gate(config):
            response = model.invoke([system, human])
        raw = str(getattr(response, "content", "") or "")
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            obj = json.loads(match.group(0))
            ok = bool(obj.get("ok"))
            if not ok:
                logger.info("Verifier rejected answer for: %s", question[:60])
            return ok, ok
    except Exception as exc:
        logger.debug("Verifier call failed, allowing answer: %s", exc)
    return True, False


def verify_llm_answer(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any],
    fill_answer: str,
    profile_excerpt: str = "",
    similar_answers: list[SimilarAnswer] | None = None,
    rag_hint: str | None = None,
) -> bool:
    """Back-compat wrapper: pass/fail only (see ``verify_llm_answer_detailed``)."""
    ok, _ = verify_llm_answer_detailed(
        config,
        question=question,
        field=field,
        fill_answer=fill_answer,
        profile_excerpt=profile_excerpt,
        similar_answers=similar_answers,
        rag_hint=rag_hint,
    )
    return ok


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _option_numeric_bounds(opt: str) -> tuple[float, float] | None:
    """Parse an option into a numeric [lo, hi] range (inf for open ends).

    Handles "1-3 years", "5 to 10", "more than 8" / "8+", "less than 6" / "<6",
    "up to 3", and bare values like "6 years" / "1 month".
    """
    o = opt.lower()
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", o)]
    if not nums:
        if re.search(r"immediate|immediately|right away|available now|asap|already joined", o):
            return (0.0, 0.0)
        return None
    if len(nums) >= 2 and re.search(r"-|–|—|\bto\b|\bbetween\b", o):
        return (min(nums[0], nums[1]), max(nums[0], nums[1]))
    if re.search(r"more than|greater than|above|over|at least|minimum|\bmin\b", o) or re.search(r"\d+\s*\+", o):
        return (nums[0], float("inf"))
    if re.search(
        r"less than|under|below|up\s*to|upto|at most|maximum|\bmax\b|within|"
        r"or\s*less|or\s*fewer|\bless\b|\bfewer\b|<",
        o,
    ):
        return (0.0, nums[0])
    return (nums[0], nums[0])


def _numeric_answer_value(answer: str) -> float | None:
    """A single numeric magnitude if the answer is essentially just a number."""
    a = answer.strip().lower()
    nums = re.findall(r"\d+(?:\.\d+)?", a)
    if len(nums) != 1:
        return None
    # Reject answers that are clearly words with an incidental number.
    leftover = re.sub(r"\d+(?:\.\d+)?", "", a)
    leftover = re.sub(
        r"\b(years?|yrs?|year|months?|mos?|days?|lpa|lacs?|lakhs?|inr|rs|k|cr|crores?|"
        r"immediate|notice|period|ctc|exp|experience|of|in|the|approx|about|around|"
        r"plus|\+|\.|,|/|-|to)\b",
        "",
        leftover,
    ).strip()
    if leftover:
        return None
    return float(nums[0])


def _deterministic_option_match(answer: str, opts: list[str]) -> str | None:
    """Reliable, non-LLM matches: exact-normalized text or numeric range/value."""
    norm_answer = _normalize_for_match(answer)
    if norm_answer:
        for opt in opts:
            if _normalize_for_match(opt) == norm_answer:
                return opt

    val = _numeric_answer_value(answer)
    if val is not None:
        containing: list[tuple[float, float, str]] = []
        for opt in opts:
            bounds = _option_numeric_bounds(opt)
            if bounds and bounds[0] <= val <= bounds[1]:
                lo, hi = bounds
                containing.append((lo, hi - lo, opt))
        if containing:
            # When the value sits on a shared boundary (in >1 band, e.g. 4 in both
            # "2-4" and "4-6"), prefer the higher band — the one with the larger
            # lower bound — so we report the next-max range. Tightest width breaks
            # any remaining tie.
            containing.sort(key=lambda t: (-t[0], t[1]))
            return containing[0][2]
    return None


_ZERO_OPTION_TEXT = re.compile(
    r"\bfresher\b|no experience|not applicable|\bn/?a\b|\bnil\b|\bzero\b|"
    r"\bnone\b|\bnever\b|no relevant|haven't|have not|do not have|don't have",
    re.I,
)


def _option_represents_zero(opt: str) -> bool:
    """True if the option covers 'zero' — 0 inside its numeric range, or zero-ish text.

    Used to decide whether a "no experience" (0) answer has any honest option to map
    onto (e.g. "0-2 years", "Less than 1 year", "Fresher", "None").
    """
    bounds = _option_numeric_bounds(opt)
    if bounds is not None:
        return bounds[0] <= 0 <= bounds[1]
    return bool(_ZERO_OPTION_TEXT.search(opt))


def _numeric_nearest_option(answer: str, opts: list[str]) -> str | None:
    """Closest numeric option when none strictly contains the value."""
    val = _numeric_answer_value(answer)
    if val is None:
        return None
    best: str | None = None
    best_dist = float("inf")
    for opt in opts:
        bounds = _option_numeric_bounds(opt)
        if not bounds:
            continue
        lo, hi = bounds
        if lo <= val <= hi:
            dist = 0.0
        else:
            hi_dist = abs(val - hi) if hi != float("inf") else float("inf")
            dist = min(abs(val - lo), hi_dist)
        if dist < best_dist:
            best_dist = dist
            best = opt
    return best


def map_answer_to_option(
    config: AppConfig,
    *,
    question: str,
    options: list[str],
    answer: str,
) -> str | None:
    """Map our stored answer onto exactly one of the question's options.

    Used only to satisfy a form whose option wording differs from our answer
    (e.g. answer "1" vs option "1 Month", answer "7" vs option "6-8 years").
    Returns one of ``options`` verbatim, or None. Does NOT change the saved
    answer — this is a fill-time format conversion only.

    Strategy: deterministic exact/numeric match first (LLMs are unreliable at
    "is 7 within 6-8?"), then a strongly-constrained LLM prompt that must pick
    the single nearest option, then a numeric-nearest fallback.
    """
    opts = [str(o).strip() for o in options if str(o).strip()]
    if not opts or not answer.strip():
        return None

    # Never inflate "no experience" into a positive band. If the answer is 0 (a
    # skill we don't have / 0 years) and no option honestly represents zero, refuse
    # to map so the question is left unanswered/queued rather than claiming, say,
    # "2-4 years" of Salesforce. Notice-period style "0 -> Immediate / 15 days or
    # less" still maps because those options DO represent zero.
    if _numeric_answer_value(answer) == 0 and not any(_option_represents_zero(o) for o in opts):
        logger.info(
            "Answer %r is zero experience but no zero option in %r — not mapping (avoid overstating) for: %s",
            answer[:40],
            opts,
            question[:50],
        )
        return None

    deterministic = _deterministic_option_match(answer, opts)
    if deterministic is not None:
        logger.info(
            "Mapped answer %r -> option %r (exact/numeric) for: %s",
            answer[:40],
            deterministic[:40],
            question[:50],
        )
        return deterministic

    model = _chat_model(config)
    if model is None:
        return _numeric_nearest_option(answer, opts)

    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(opts))
    system = SystemMessage(
        content=(
            "You convert a candidate's existing answer into exactly ONE of a "
            "form's preset options. You never change the meaning of the answer — "
            "you only choose the option that is the closest/nearest match.\n"
            "Rules:\n"
            "- Always pick the SINGLE nearest option, even if the wording differs.\n"
            "- For numbers, pick the option whose numeric range CONTAINS the "
            "answer; if none contains it, pick the closest range.\n"
            '  Examples: answer 7 with ["1-3 years","6-8 years","8+ years"] -> 2; '
            'answer 0 with ["Immediate","15 Days or less","1 Month"] -> 1 (0 days = immediate); '
            'answer 12 (days) with ["15 Days or less","1 Month","2 Months"] -> 1.\n'
            '- For yes/no options, map an affirmative/relevant answer to "Yes" '
            'and a negative/irrelevant one to "No" '
            '(e.g. question "Are you based in Bengaluru?" answer "Bengaluru" -> Yes).\n'
            "- Only return index 0 if NO option is even loosely related to the "
            "answer.\n"
            'Return STRICT JSON only: {"index": N} (1-based option number, or 0).'
        )
    )
    human = HumanMessage(
        content=(
            f"Question: {question}\n"
            f"Candidate's answer: {answer}\n"
            f"Options:\n{numbered}\n"
            "Return the JSON for the single nearest option."
        )
    )
    try:
        with _ollama_gate(config):
            response = model.invoke([system, human])
        raw = str(getattr(response, "content", "") or "")
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            idx = int(json.loads(match.group(0)).get("index", 0))
            if 1 <= idx <= len(opts):
                chosen = opts[idx - 1]
                logger.info(
                    "LLM mapped answer %r -> option %r for: %s",
                    answer[:40],
                    chosen[:40],
                    question[:50],
                )
                return chosen
    except Exception as exc:
        logger.debug("LLM option mapping failed: %s", exc)

    fallback = _numeric_nearest_option(answer, opts)
    if fallback is not None:
        logger.info(
            "Mapped answer %r -> option %r (numeric nearest) for: %s",
            answer[:40],
            fallback[:40],
            question[:50],
        )
    return fallback


def select_options_for_question(
    config: AppConfig,
    *,
    question: str,
    options: list[str],
    multi: bool = False,
    extra_context: str | None = None,
) -> list[str]:
    """Pick the best option(s) for a choice question using the candidate profile.

    This is the primary answer-determination path for options-based questions:
    the options are fed straight into the decision and the model returns the
    best one(s) from the list — rather than generating free text and converting
    it afterwards. ``extra_context`` may carry RAG/rule hints and prior answers.
    Returns option strings verbatim (a subset of ``options``) — at most one for
    single-select, possibly several for multi-select. Empty if it can't decide.
    """
    opts = [str(o).strip() for o in options if str(o).strip()]
    if not opts:
        return []
    model = _chat_model(config)
    if model is None:
        return []
    profile = _build_application_context(config)
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(opts))
    count_hint = "Return every option that applies to the candidate" if multi else "Return exactly one option"
    system = SystemMessage(
        content=(
            "You select the option(s) that best fit the candidate for a "
            "job-application form, using ONLY the verified candidate profile "
            "(and any hints) below. Do not invent facts. "
            f"{count_hint}. Always choose the closest option(s) rather than "
            "abstaining unless truly none apply. "
            'Return strict JSON only: {"indices": [N, ...]} with 1-based option '
            'numbers, or {"indices": []} if none fit. Choose only from the '
            "listed options."
        )
    )
    hint_block = f"\n=== HINTS (rules / prior answers) ===\n{extra_context}\n" if extra_context else ""
    human = HumanMessage(
        content=(
            f"=== VERIFIED CANDIDATE PROFILE ===\n{profile}\n"
            f"{hint_block}\n"
            f"Question: {question}\n"
            f"Options:\n{numbered}\n"
            f"{count_hint} that best fits the candidate."
        )
    )
    try:
        with _ollama_gate(config):
            response = model.invoke([system, human])
        raw = str(getattr(response, "content", "") or "")
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return []
        data = json.loads(match.group(0))
        idxs = data.get("indices")
        if idxs is None:
            idxs = data.get("index")
        if isinstance(idxs, int):
            idxs = [idxs]
        chosen: list[str] = []
        for raw_idx in idxs or []:
            try:
                n = int(raw_idx)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= len(opts) and opts[n - 1] not in chosen:
                chosen.append(opts[n - 1])
        if not multi:
            chosen = chosen[:1]
        if chosen:
            logger.info(
                "LLM selected option(s) %s for: %s",
                [c[:30] for c in chosen],
                question[:50],
            )
        return chosen
    except Exception as exc:
        logger.debug("LLM option selection failed: %s", exc)
        return []


def generate_join_reason(
    config: AppConfig,
    *,
    job_title: str = "",
    company: str = "",
    jd: str = "",
    company_about: str = "",
    max_words: int = 120,
) -> str | None:
    """Tailor a short "why do you want to join us" answer per job.

    Uses the verified candidate profile plus this job's JD and the company's
    "about" text so the answer is specific to the role/company. Returns plain
    first-person prose (no greeting/signature). ``None`` if the LLM is unavailable.
    """
    model = _chat_model(config)
    if model is None:
        return None

    profile = _build_application_context(config)
    org = company.strip() or "the company"
    role = job_title.strip() or "this role"

    context_parts: list[str] = []
    about = (company_about or "").strip()
    if about:
        context_parts.append(f"=== ABOUT {org.upper()} ===\n{about[:1500]}")
    jd_text = (jd or "").strip()
    if jd_text:
        context_parts.append(f"=== JOB DESCRIPTION ({role}) ===\n{jd_text[:2500]}")
    context_block = "\n\n".join(context_parts)

    system = SystemMessage(
        content=(
            f"You write a short, sincere answer for {config.user.name} to the application "
            f"question 'Why do you want to join {org}?'.\n"
            "Use ONLY the verified candidate profile, the job description, and the company "
            "'about' text below. Never invent employers, credentials, or metrics.\n"
            "Tie the candidate's real experience to specifics of this role and company "
            "(mission, product, tech, or domain) — do not be generic.\n"
            f"Write in first person, {max_words} words or fewer, 2-4 sentences. "
            "Return only the answer text — no greeting, no signature, no quotes, no preamble."
        )
    )
    human = HumanMessage(
        content=(
            f"=== VERIFIED CANDIDATE PROFILE ===\n{profile}\n\n"
            f"{context_block}\n\n"
            f"Question: Why do you want to join {org} as {role}?"
        ).strip()
    )

    try:
        with _ollama_gate(config):
            response = model.invoke([system, human])
        raw = str(getattr(response, "content", "") or "").strip()
        if not raw:
            return None
        text = _QUOTE_WRAP.sub(r"\1", raw).strip()
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words]).rstrip(",;:") + "."
        logger.info("Tailored join-reason answer (%d words) for %s @ %s", len(text.split()), role[:40], org[:40])
        return text or None
    except Exception as exc:
        logger.warning("Join-reason generation failed for %s @ %s: %s", role[:40], org[:40], exc)
        return None


def generate_cover_letter_llm(
    config: AppConfig,
    *,
    job_title: str = "",
    company: str = "",
    jd: str = "",
    company_about: str = "",
    reference: str = "",
    include_ctc: bool = True,
    max_words: int = 400,
) -> str | None:
    """Write a tailored cover letter per job.

    Uses the verified candidate profile (resume facts), the user's existing
    reference cover letter (for tone/structure), this job's JD, and the company's
    "about" text so the letter is specific to the role and company. Returns the
    full letter (greeting + body + signature). ``None`` if the LLM is unavailable.
    """
    # A 400-word letter needs ~550+ tokens; the global max_tokens (often 256, tuned
    # for short screening answers) truncates the letter mid-sentence. Give the
    # letter its own budget scaled to the word target, with headroom.
    cover_letter_tokens = max(config.llm.max_tokens, int(max_words * 2) + 64)
    model = _chat_model(config, num_predict=cover_letter_tokens)
    if model is None:
        return None

    profile = _build_application_context(config)
    org = company.strip() or "your organisation"
    role = job_title.strip() or "this role"

    context_parts: list[str] = []
    ref = (reference or "").strip()
    if ref:
        context_parts.append(
            f"=== REFERENCE COVER LETTER (adapt tone & structure, do not copy verbatim) ===\n{ref[:3000]}"
        )
    about = (company_about or "").strip()
    if about:
        context_parts.append(f"=== ABOUT {org.upper()} ===\n{about[:1500]}")
    jd_text = (jd or "").strip()
    if jd_text:
        context_parts.append(f"=== JOB DESCRIPTION ({role}) ===\n{jd_text[:2500]}")
    context_block = "\n\n".join(context_parts)

    ctc_rule = (
        "Include one sentence stating the candidate's current and expected CTC from the profile."
        if include_ctc
        else "Do not mention salary or CTC."
    )

    system = SystemMessage(
        content=(
            f"You write a tailored job-application cover letter for {config.user.name} "
            f"applying to the {role} role at {org}.\n"
            "Use ONLY the verified candidate profile, the reference cover letter, the job "
            "description, and the company 'about' text below. Never invent employers, "
            "credentials, or metrics.\n"
            "Adapt the reference cover letter's tone, but tailor the content to "
            "this specific role and company (mission, product, tech, or domain) — do not be "
            "generic and never leave placeholders like {{company}}.\n"
            "Write the letter as flowing prose in 3-5 short paragraphs. Do NOT use emojis, "
            "bullet points, dashes as list markers, numbered lists, headings, or resume-style "
            "section labels (e.g. company names as headers or 'Tech used:' lines). It must read "
            "like a letter, not a resume.\n"
            f"{ctc_rule}\n"
            f"Write in first person, {max_words} words or fewer. Open with a greeting addressed "
            "to the company/hiring manager and close with the candidate's name. "
            "Return only the cover letter text — no commentary, no markdown, no quotes."
        )
    )
    human = HumanMessage(
        content=(
            f"=== VERIFIED CANDIDATE PROFILE ===\n{profile}\n\n"
            f"{context_block}\n\n"
            f"Write the cover letter for the {role} role at {org}."
        ).strip()
    )

    try:
        with _ollama_gate(config):
            response = model.invoke([system, human])
        raw = str(getattr(response, "content", "") or "").strip()
        if not raw:
            return None
        text = _QUOTE_WRAP.sub(r"\1", raw).strip()
        text = _sanitize_cover_letter(text)
        if not text:
            return None
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words]).rstrip(",;:") + "."
        logger.info("Tailored LLM cover letter (%d words) for %s @ %s", len(text.split()), role[:40], org[:40])
        return text or None
    except Exception as exc:
        logger.warning("LLM cover letter generation failed for %s @ %s: %s", role[:40], org[:40], exc)
        return None


def generate_llm_decision(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any] | None = None,
    similar_answers: list[SimilarAnswer] | None = None,
    rag_hint: str | None = None,
    free_tier: Any | None = None,
    profile_context: str | None = None,
) -> LLMDecision | None:
    """Answer a screening question with LLM and include confidence [0..1]."""
    model = _chat_model(config)
    if not model:
        return None

    field = field or {"kind": "text", "label": question}
    from .application_questions import enrich_field_for_llm

    field = enrich_field_for_llm(field)
    profile = profile_context or _build_application_context(config)
    if similar_answers is None and free_tier is not None and free_tier.similar_answers:
        similar_answers = free_tier.similar_answers
    if similar_answers is None and config.application.rag_answer_questions:
        similar_answers = retrieve_similar_answers(config, question)
    examples = _format_similar_answers(similar_answers) if similar_answers else ""

    hint_parts: list[str] = []
    if free_tier is not None:
        tier_block = _format_free_tier_hints(free_tier)
        if tier_block:
            hint_parts.append(tier_block)
    elif rag_hint and str(rag_hint).strip():
        hint_parts.append(
            f"\n\n=== RAG SUGGESTION (from application_facts / rules) ===\n"
            f"{str(rag_hint).strip()}\n"
            "Use this when it fits the question and field; lower confidence if you disagree."
        )
    hints_block = "".join(hint_parts)
    examples_block = f"\n\n{examples}" if examples else ""

    system = SystemMessage(
        content=(
            f"You help {config.user.name} answer job application screening questions.\n"
            "Use ONLY the verified candidate profile below (resume_facts.yaml + application_facts). "
            "Never invent employers, credentials, PAN, UAN, or metrics.\n"
            "When similar prior answers are provided, prefer adapting the highest-similarity "
            "answer that fits the current field/options — do not copy unrelated answers.\n"
            "When pre-computed hints or RAG suggestions are provided, prefer them when they "
            "match the profile and field format — rate confidence high only when correct.\n"
            "The FORM FIELD block describes control kind and expected value type — "
            "your answer MUST match that format (e.g. ctc_numeric = digits only, "
            "single_choice = exact option label).\n"
            "Return strict JSON only: "
            '{"answer":"...","canonical":"...","confidence":0.0-1.0}.\n'
            "answer = exact option label when options are given (for form fill). "
            "canonical = specific factual value to remember (e.g. '4' not '<6 years').\n"
            "Confidence means probability the answer is correct for this specific field/options.\n"
            "If unsure, lower confidence; never inflate confidence.\n"
            f"{_field_instructions(field)}"
        )
    )
    human = HumanMessage(content=(f"{profile}{examples_block}{hints_block}\n\nQuestion: {question}").strip())

    try:
        with _ollama_gate(config):
            response = model.invoke([system, human])
        raw = getattr(response, "content", "") or ""
        parsed = _parse_llm_json(str(raw))
        if parsed:
            answer, confidence, canonical = parsed
            answer = _normalize_llm_answer(answer, field)
            if answer:
                from .answers.format_finalize import finalize_answer_for_field

                finalized = finalize_answer_for_field(
                    question,
                    field,
                    config,
                    raw_answer=answer,
                    canonical=canonical,
                )
                if not finalized:
                    return None
                fill, stored = finalized
                # Verification is deferred to the acceptance step (llm_decision_acceptable)
                # so the verifier model is only invoked when RAG/vector did NOT already
                # corroborate the answer — avoiding a redundant model call.
                logger.info("LLM answer for: %s (confidence=%.2f)", question[:60], confidence)
                return LLMDecision(answer=fill, confidence=confidence, canonical=stored)
        # Backward fallback if provider returns plain text
        answer = _normalize_llm_answer(str(raw), field)
        if answer:
            from .answers.format_finalize import finalize_answer_for_field

            finalized = finalize_answer_for_field(question, field, config, raw_answer=answer)
            if not finalized:
                return None
            fill, stored = finalized
            confidence = config.llm.plain_text_confidence
            logger.info(
                "LLM plain-text answer for: %s (confidence=%.2f)",
                question[:60],
                confidence,
            )
            return LLMDecision(answer=fill, confidence=confidence, canonical=stored)
    except Exception as exc:
        logger.warning("LLM answer failed for %s: %s", question[:60], exc)
    return None


def generate_llm_answer(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any] | None = None,
    similar_answers: list[SimilarAnswer] | None = None,
) -> str | None:
    decision = generate_llm_decision(
        config,
        question=question,
        field=field,
        similar_answers=similar_answers,
    )
    if not decision:
        return None
    return decision.answer
