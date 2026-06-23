from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

from .config import AppConfig
from .memory import load_memory
from .profile_data import build_resume_context, load_resume_facts
from .resume_text import relevant_resume_excerpt
from .question_groups import classify_question
from .rag_answers import load_application_facts

logger = logging.getLogger("job_apply")

_QUOTE_WRAP = re.compile(r'^["\'](.+)["\']$', re.S)
_FAISS_STORE_NAME = "user_memory_qa"
_FAISS_CACHE: dict[str, tuple[float, Any]] = {}


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


def _strip_llm_text(text: str) -> str:
    answer = (text or "").strip()
    m = _QUOTE_WRAP.match(answer)
    if m:
        answer = m.group(1).strip()
    answer = re.sub(r"^(answer|response)\s*:\s*", "", answer, flags=re.I).strip()
    return answer


def _field_instructions(field: dict[str, Any]) -> str:
    kind = str(field.get("kind", "text"))
    options = [str(o).strip() for o in field.get("options", []) if str(o).strip()]
    lines: list[str] = [f"Field type: {kind}"]

    if options:
        lines.append(f"Options (pick exactly from this list): {', '.join(options)}")
        if kind in ("radio", "checkbox"):
            lines.append("Reply with exactly one option label from the list.")
            if any(re.search(r"<\s*\d+|\d+\s*[-–]\s*\d+|\d+\s*\+", o) for o in options):
                lines.append(
                    "For year-range options, set canonical to the candidate's specific years "
                    "(e.g. '4' or '4 years') from profile/resume — not the range label."
                )
        elif kind == "checkbox_group":
            lines.append("Reply with comma-separated option labels from the list.")
    elif kind in ("radio", "checkbox"):
        lines.append("Reply with exactly Yes or No.")
    elif kind in ("input", "textarea", "text"):
        lines.append("Reply with a short, direct answer (1-3 sentences max unless numeric).")

    label = str(field.get("label", ""))
    if re.search(
        r"\b(associated with|previously employed|employed by|worked (?:at|for)|"
        r"received an offer from|military spouse)\b",
        label,
        re.I,
    ):
        lines.append(
            "This is a past-employer or eligibility check. Use the work experience list "
            "in the profile. Answer Yes ONLY if the named company appears in that list; "
            "otherwise answer No. Reply with exactly Yes or No."
        )

    return "\n".join(lines)


def _build_application_context(config: AppConfig, question: str = "") -> str:
    facts = load_resume_facts(config.base_dir)
    app_facts = load_application_facts(config.base_dir)
    comp = config.compensation
    resume_excerpt = relevant_resume_excerpt(config, question, max_chars=3500)

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

    for key, value in app_facts.items():
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            lines.append(f"- {key}: {', '.join(str(v) for v in value)}")
        else:
            lines.append(f"- {key}: {value}")

    if resume_excerpt:
        lines.extend(
            [
                "",
                "Resume excerpt (from uploaded PDF — use ONLY this for role/skill/project details):",
                resume_excerpt,
            ]
        )

    if facts.experience:
        lines.extend(["", "Work experience (employers — use for past/current employer Yes/No checks):"])
        for role in facts.experience:
            company = str(role.get("company", "")).strip()
            title = str(role.get("title", "")).strip()
            period = str(role.get("period", "")).strip()
            if company:
                line = f"- {title} at {company}" if title else f"- {company}"
                if period:
                    line += f" ({period})"
                lines.append(line)

    return "\n".join(lines)


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
    p = config.base_dir / "data" / "user_memory.json"
    return p.stat().st_mtime if p.exists() else 0.0


def _get_faiss_store(config: AppConfig):
    if not config.llm.use_faiss_memory:
        return None

    try:
        from langchain_community.vectorstores import FAISS
        from langchain_huggingface import HuggingFaceEmbeddings
    except Exception as exc:
        logger.debug("FAISS dependencies unavailable, falling back: %s", exc)
        return None

    idx_path = _faiss_index_path(config)
    cache_key = str(idx_path.resolve())
    mtime = _memory_mtime(config)
    cached = _FAISS_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        embeddings = HuggingFaceEmbeddings(model_name=config.llm.embeddings_model)
    except Exception as exc:
        logger.warning("FAISS embedding model unavailable, falling back: %s", exc)
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
    limit = k if k is not None else config.llm.rag_top_k
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
                        score=_distance_to_similarity(distance),
                    )
                )
            if picked:
                return picked[:limit]
        except Exception as exc:
            logger.debug("FAISS similarity_search failed, fallback to lexical: %s", exc)

    return _lexical_similar_answers(config, question, limit=limit)


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
    return [
        SimilarAnswer(question=q, answer=a, score=score)
        for score, q, a in scored[:limit]
    ]


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
    answers = similar_answers if similar_answers is not None else retrieve_similar_answers(
        config, question
    )
    return _format_similar_answers(answers)


def _normalize_llm_answer(answer: str, field: dict[str, Any]) -> str:
    from .application_questions import _normalize_to_option

    text = _strip_llm_text(answer)
    if not text:
        return ""

    kind = str(field.get("kind", "text"))
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


def _chat_model(config: AppConfig) -> Any | None:
    llm = config.llm
    if not llm.enabled:
        return None
    provider = llm.provider.strip().lower()
    if provider == "groq":
        api_key = (llm.api_key or os.environ.get("GROQ_API_KEY", "")).strip()
        if not api_key:
            logger.warning("LLM enabled but no Groq API key (set llm.api_key or GROQ_API_KEY)")
            return None
        return ChatGroq(
            api_key=api_key,
            model=llm.model,
            temperature=llm.temperature,
            max_tokens=llm.max_tokens,
        )

    if provider == "freemodel":
        # FreeModel docs: OpenAI-compatible API with base URL https://api.freemodel.dev
        api_key = (
            llm.api_key
            or os.environ.get("FREEMODEL_API_KEY", "")
            or os.environ.get("OPENAI_API_KEY", "")
        ).strip()
        if not api_key:
            logger.warning(
                "LLM enabled but no FreeModel API key (set llm.api_key, FREEMODEL_API_KEY, or OPENAI_API_KEY)"
            )
            return None
        base_url = (llm.base_url or "https://api.freemodel.dev").rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        return ChatOpenAI(
            api_key=api_key,
            base_url=base_url,
            model=llm.model or "gpt-5.5",
            temperature=llm.temperature,
            max_tokens=llm.max_tokens,
        )

    logger.warning("Unsupported llm.provider=%r; expected groq or freemodel", llm.provider)
    return None


def generate_llm_decision(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any] | None = None,
    jd: str = "",
    job_title: str = "",
    company: str = "",
    similar_answers: list[SimilarAnswer] | None = None,
) -> LLMDecision | None:
    """Answer a screening question with LLM and include confidence [0..1]."""
    model = _chat_model(config)
    if not model:
        return None

    field = field or {"kind": "text", "label": question}
    profile = _build_application_context(config, question=question)
    if similar_answers is None and config.application.rag_answer_questions:
        similar_answers = retrieve_similar_answers(config, question)
    examples = _memory_examples(config, question, similar_answers=similar_answers)
    job_bits = []
    if job_title:
        job_bits.append(f"Role: {job_title}")
    if company:
        job_bits.append(f"Company: {company}")
    if jd:
        job_bits.append(f"Job description excerpt:\n{jd[:2500]}")

    system = SystemMessage(
        content=(
            f"You help {config.user.name} answer job application screening questions.\n"
            "Use ONLY the verified candidate profile below. Never invent employers, "
            "credentials, PAN, UAN, or metrics.\n"
            "When similar prior answers are provided, prefer adapting the highest-similarity "
            "answer that fits the current field/options — do not copy unrelated answers.\n"
            "Return strict JSON only: "
            '{"answer":"...","canonical":"...","confidence":0.0-1.0}.\n'
            "answer = exact option label when options are given (for form fill). "
            "canonical = specific factual value to remember (e.g. '4' not '<6 years').\n"
            "Confidence means probability the answer is correct for this specific field/options.\n"
            "If unsure, lower confidence; never inflate confidence.\n"
            f"{_field_instructions(field)}"
        )
    )
    human = HumanMessage(
        content=(
            f"{profile}\n\n"
            f"{examples}\n\n"
            f"{' | '.join(job_bits) if job_bits else ''}\n\n"
            f"Question: {question}"
        ).strip()
    )

    try:
        response = model.invoke([system, human])
        raw = getattr(response, "content", "") or ""
        parsed = _parse_llm_json(str(raw))
        if parsed:
            answer, confidence, canonical = parsed
            answer = _normalize_llm_answer(answer, field)
            if answer:
                from .application_questions import canonicalize_stored_answer

                stored = canonical or canonicalize_stored_answer(
                    question, answer, field, config
                )
                logger.info("LLM answer for: %s (confidence=%.2f)", question[:60], confidence)
                return LLMDecision(answer=answer, confidence=confidence, canonical=stored)
        # Backward fallback if provider returns plain text
        answer = _normalize_llm_answer(str(raw), field)
        if answer:
            logger.info("LLM plain-text answer for: %s (confidence=0.50)", question[:60])
            return LLMDecision(answer=answer, confidence=0.5)
    except Exception as exc:
        logger.warning("LLM answer failed for %s: %s", question[:60], exc)
    return None


def generate_llm_answer(
    config: AppConfig,
    *,
    question: str,
    field: dict[str, Any] | None = None,
    jd: str = "",
    job_title: str = "",
    company: str = "",
    similar_answers: list[SimilarAnswer] | None = None,
) -> str | None:
    decision = generate_llm_decision(
        config,
        question=question,
        field=field,
        jd=jd,
        job_title=job_title,
        company=company,
        similar_answers=similar_answers,
    )
    if not decision:
        return None
    return decision.answer
