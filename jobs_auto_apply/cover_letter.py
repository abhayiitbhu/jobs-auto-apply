from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from playwright.async_api import Page

from .jd import extract_job_description
from .profile_data import ResumeFacts, load_resume_facts

if TYPE_CHECKING:
    from .config import AppConfig
    from .utils import JobListing

logger = logging.getLogger("job_apply")

SKILL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Java", re.compile(r"\bjava\b(?!\s*script)", re.I)),
    ("Spring Boot", re.compile(r"spring\s*boot|springboot", re.I)),
    ("Python", re.compile(r"\bpython\b", re.I)),
    ("FastAPI", re.compile(r"\bfastapi\b", re.I)),
    ("Flask", re.compile(r"\bflask\b", re.I)),
    ("AWS", re.compile(r"\baws\b|amazon web services", re.I)),
    ("Kafka", re.compile(r"\bkafka\b", re.I)),
    ("Redis", re.compile(r"\bredis\b", re.I)),
    ("MySQL", re.compile(r"\bmysql\b", re.I)),
    ("MongoDB", re.compile(r"\bmongodb\b", re.I)),
    ("Docker", re.compile(r"\bdocker\b", re.I)),
    ("Kubernetes", re.compile(r"\bkubernetes\b|\bk8s\b", re.I)),
    ("microservices", re.compile(r"\bmicroservices?\b", re.I)),
    ("OpenSearch", re.compile(r"\bopensearch\b|\belasticsearch\b", re.I)),
    ("CI/CD", re.compile(r"\bci/?cd\b", re.I)),
    ("Grafana", re.compile(r"\bgrafana\b", re.I)),
    ("Node.js", re.compile(r"\bnode\.?js\b", re.I)),
]


def _match_skills(jd: str, profile_skills: list[str]) -> list[str]:
    matched: list[str] = []
    seen: set[str] = set()
    for skill in profile_skills:
        if skill in seen:
            continue
        skill_lower = skill.lower()
        if skill_lower in jd.lower():
            matched.append(skill)
            seen.add(skill)
            continue
        for label, pattern in SKILL_PATTERNS:
            if label.lower() == skill_lower and pattern.search(jd):
                matched.append(skill)
                seen.add(skill)
                break
    return matched[:6]


def _format_ctc_line(config: AppConfig) -> str:
    comp = config.compensation
    if not config.cover_letter.include_ctc:
        return ""
    return (
        f"My current CTC is {comp.current_ctc_lpa:.0f} LPA "
        f"({comp.current_fixed_lpa:.0f}L fixed + {comp.current_variable_lpa:.0f}L variable + "
        f"{comp.current_esops_lpa:.0f}L ESOPs), and I am expecting around {comp.expected_ctc_lpa:.0f} LPA."
    )


def _skills_sentence(skills: list[str]) -> str:
    if not skills:
        return "Java Spring Boot, Python FastAPI/Flask, AWS, Kafka, and modern DevOps practices"
    return ", ".join(skills[:5])


def _role_paragraph(role: dict) -> str:
    company = str(role.get("company", ""))
    period = str(role.get("period", ""))
    title = str(role.get("title", ""))
    highlights = list(role.get("highlights", []))
    if not highlights:
        return f"At {company} ({period}), I worked as {title}."
    lead = highlights[0]
    sentence = f"At {company} ({period}), I {lead[0].lower()}{lead[1:]}"
    if len(highlights) > 1:
        extras = [f"{h[0].lower()}{h[1:]}" for h in highlights[1:3]]
        sentence += ", including " + ", ".join(extras)
    return sentence + "."


def _pick_roles_for_jd(facts: ResumeFacts, jd: str, *, limit: int = 2) -> list[dict]:
    jd_lower = jd.lower()
    scored: list[tuple[int, dict]] = []
    for role in facts.experience:
        score = 0
        for highlight in role.get("highlights", []):
            for word in re.findall(r"[a-zA-Z]{4,}", highlight.lower()):
                if word in jd_lower:
                    score += 1
        scored.append((score, role))
    scored.sort(key=lambda x: x[0], reverse=True)
    chosen = [role for score, role in scored if score > 0][:limit]
    if not chosen:
        chosen = facts.experience[:limit]
    return chosen


def _load_cover_letter_reference(config: AppConfig) -> str:
    path = config.cover_letter_reference_path
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", re.S)


def strip_markdown_emphasis(text: str) -> str:
    """Remove Markdown bold/italic markers for plain-text form fields.

    Cover letters keep ``**bold**`` markers so console/review previews can show
    emphasis, but raw application textareas render those asterisks literally.
    Strip them right before filling a form so the submitted letter is clean.
    """
    if not text:
        return text
    text = _BOLD_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    return text


def _format_phone_display(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+91 {digits[:5]} {digits[5:]}"
    if digits.startswith("91") and len(digits) == 12:
        return f"+91 {digits[2:7]} {digits[7:]}"
    return phone


def _signature_block(facts: ResumeFacts) -> str:
    lines = ["Best regards,", facts.name]
    if facts.phone:
        lines.append(_format_phone_display(facts.phone))
    return "\n".join(lines)


def _ensure_signature(text: str, facts: ResumeFacts) -> str:
    """Ensure cover letter ends with name and phone."""
    text = text.rstrip()
    phone = (facts.phone or "").strip()
    name = (facts.name or "").strip()
    if not name:
        return text
    display_phone = _format_phone_display(phone) if phone else ""
    if display_phone and (display_phone in text or phone in text):
        return text
    if name in text and display_phone:
        return f"{text}\n{display_phone}"
    return f"{text}\n\n{_signature_block(facts)}"


def _adapt_reference_cover_letter(
    config: AppConfig,
    *,
    job: JobListing,
    jd: str,
    facts: ResumeFacts,
    reference: str,
) -> str:
    """Tailor the user's reference cover letter for a specific role/company/JD."""
    org = job.company or "your organisation"
    title = job.title or "this role"
    skills = _match_skills(jd, config.profile.core_skills)
    skill_phrase = (
        ", ".join(f"**{skill}**" for skill in skills[:3])
        if skills
        else "**backend systems** and **cloud infrastructure**"
    )

    text = reference.replace("{{title}}", title).replace("{{company}}", org).replace("{{skills}}", skill_phrase)

    # Legacy plain-text reference (pre-placeholder)
    text = text.replace(
        "I am excited to apply for the Senior Backend Developer position at your organisation.",
        f"I am excited to apply for the {title} position at {org}.",
    )
    text = text.replace("your organisation", org)

    drawn_new = (
        f"I am particularly drawn to {org} given its emphasis on {skill_phrase}, "
        "and I am confident my track record of delivering reliable, low-latency systems "
        "would let me contribute meaningfully from day one."
    )
    for old_drawn in (
        "I am particularly drawn to your organisation because of your innovative work in scalable "
        "solutions, and I am confident my track record of delivering reliable, low-latency systems and "
        "mentoring teams would allow me to contribute meaningfully from day one.",
        "I am particularly drawn to Floe Labs given its emphasis on Python, and I am confident my "
        "track record of delivering reliable, low-latency systems would let me contribute meaningfully from day one.",
    ):
        text = text.replace(old_drawn.replace("your organisation", org), drawn_new)
        text = text.replace(old_drawn, drawn_new)

    ctc = _format_ctc_line(config)
    if ctc and ctc not in text:
        closing = "I would welcome the opportunity"
        if closing in text:
            text = text.replace(f"\n{closing}", f"\n\n{ctc}\n\n{closing}", 1)

    return _ensure_signature(text, facts)


def _company_about_from_job(job: JobListing) -> str:
    meta = getattr(job, "meta", None)
    if isinstance(meta, dict):
        return str(meta.get("company_about", "") or "")
    return ""


def generate_cover_letter_dynamic(config: AppConfig, *, job: JobListing, jd: str) -> str:
    facts = load_resume_facts(config.base_dir)
    reference = _load_cover_letter_reference(config)

    from .llm_answers import generate_cover_letter_llm

    llm_letter = generate_cover_letter_llm(
        config,
        job_title=job.title or "",
        company=job.company or "",
        jd=jd,
        company_about=_company_about_from_job(job),
        reference=reference,
        include_ctc=config.cover_letter.include_ctc,
        max_words=config.cover_letter.max_words,
    )
    if llm_letter:
        return llm_letter

    if reference:
        return _adapt_reference_cover_letter(config, job=job, jd=jd, facts=facts, reference=reference)

    org = job.company or "your organisation"
    title = job.title or "this role"
    skills = _match_skills(jd, config.profile.core_skills)
    skill_phrase = _skills_sentence(skills)
    roles = _pick_roles_for_jd(facts, jd, limit=2)
    latest = facts.experience[0] if facts.experience else {}

    paragraphs = [
        "Dear Hiring Manager,",
        "",
        (
            f"I am excited to apply for the {title} position at {org}. "
            f"With hands-on experience building scalable microservices and distributed systems — "
            f"most recently as {latest.get('title', facts.headline)} at {latest.get('company', 'my current company')} — "
            f"I am eager to bring my expertise in {skill_phrase} to your team."
        ),
        "",
    ]
    for role in roles:
        paragraphs.append(_role_paragraph(role))
        paragraphs.append("")

    paragraphs.append(f"My foundation includes {facts.education}.")
    paragraphs.append("")

    if skills:
        paragraphs.append(
            f"I am particularly drawn to this role given its emphasis on {', '.join(skills[:3])}, "
            f"and I am confident my track record of delivering reliable, production-grade systems "
            f"would allow me to contribute meaningfully from day one."
        )
    else:
        paragraphs.append(
            "I am confident my track record of delivering reliable, production-grade systems "
            "would allow me to contribute meaningfully from day one."
        )

    ctc = _format_ctc_line(config)
    if ctc:
        paragraphs.extend(["", ctc])

    paragraphs.extend(
        [
            "",
            "I would welcome the opportunity to discuss how my experience aligns with your current goals. "
            "Thank you for considering my application—I look forward to the possibility of speaking with you soon.",
            "",
            _signature_block(facts),
        ]
    )
    return _trim_words("\n".join(paragraphs), config.cover_letter.max_words)


def _trim_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "..."


def render_static_template(config: AppConfig, *, title: str, company: str) -> str:
    template = config.cover_note
    return (
        template.replace("{{title}}", title)
        .replace("{{company}}", company or "your team")
        .replace("{{job_title}}", title)
    )


async def build_cover_letter(
    config: AppConfig,
    *,
    job: JobListing,
    page: Page | None = None,
    jd: str = "",
    prefer_precomputed: bool = True,
) -> str:
    if prefer_precomputed and job.meta.get("cover_letter"):
        return str(job.meta["cover_letter"]).strip()

    if job.description:
        jd = job.description
    elif page and not jd:
        if job.source == "wellfound":
            from .wellfound.jd import extract_wellfound_page_jd

            jd = await extract_wellfound_page_jd(page)
        if not jd:
            jd = await extract_job_description(page)

    if page and job.source == "wellfound" and not _company_about_from_job(job):
        from .wellfound.company import extract_wellfound_company_about

        about = await extract_wellfound_company_about(page, jd=jd)
        if about and isinstance(job.meta, dict):
            job.meta["company_about"] = about

    mode = config.cover_letter.mode.lower()
    company = job.company or "your team"
    title = job.title or "this role"
    facts = load_resume_facts(config.base_dir)

    if mode == "template":
        return render_static_template(config, title=title, company=company)

    if mode == "llm":
        logger.warning("cover_letter.mode llm is removed; using dynamic reference-based letters")

    if not jd:
        logger.debug(
            "No JD found for %s; generating cover letter from profile/reference/company context",
            job.url,
        )

    return _ensure_signature(generate_cover_letter_dynamic(config, job=job, jd=jd), facts)
