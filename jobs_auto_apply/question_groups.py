from __future__ import annotations

import re
from dataclasses import dataclass

from .application_questions import question_key


@dataclass(frozen=True)
class QuestionGroupDef:
    group_id: str
    title: str
    hint: str
    patterns: tuple[re.Pattern[str], ...]


def _norm(text: str) -> str:
    t = text.lower().strip()
    t = t.replace("&gt;", ">").replace("&lt;", "<")
    t = re.sub(r"[^\w\s/&+.-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _norm(text)).strip("_")[:48]


# First match wins — maps skill phrases (and full questions) to a shared group id.
_CANONICAL_SKILL_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"llm|large language|gen\s*ai|genai|gpt|langchain|openai|llama|transformer|"
            r"\brag\b|retrieval augmented|\bai\b|artificial intelligence|machine learning|\bml\b"
        ),
        "ai_ml",
    ),
    (re.compile(r"java|j2ee|spring|microservices?|jdk|core java"), "java"),
    (re.compile(r"python|fastapi|flask|django|pyspark"), "python"),
    (re.compile(r"postgresql|postgres"), "postgresql"),
    (re.compile(r"\baws\b|amazon web|ec2|lambda|s3"), "aws"),
    (re.compile(r"\.net|asp\.?net|c#|dotnet"), "dotnet"),
    (re.compile(r"\breact\b"), "react"),
    (re.compile(r"backend|software engineering"), "backend"),
    (re.compile(r"distributed systems"), "distributed_systems"),
    (re.compile(r"cdn"), "cdn"),
    (re.compile(r"mainframe|ims db"), "mainframe"),
    (re.compile(r"web\s*3|blockchain"), "blockchain"),
    (re.compile(r"security ops|security operations|secops"), "security"),
    (re.compile(r"scripting|automation|terraform|ansible"), "devops_scripting"),
    (re.compile(r"multi.?thread|concurrency"), "concurrency"),
    (re.compile(r"elastic\s*search|elasticsearch"), "elasticsearch"),
    (re.compile(r"databricks"), "databricks"),
    (re.compile(r"power\s*bi"), "power_bi"),
    (re.compile(r"full\s*stack|fullstack"), "fullstack"),
    (re.compile(r"architecture"), "architecture"),
    (re.compile(r"oops|object oriented"), "oops"),
)


def _canonical_skill_id(phrase: str, *, full_norm: str = "") -> str:
    """Normalize skill wording so variants share one memory / pending group."""
    for text in (phrase, full_norm):
        if not text:
            continue
        n = _norm(text)
        for pattern, canonical in _CANONICAL_SKILL_RULES:
            if pattern.search(n):
                return canonical
    slug = _slug(phrase) or _slug(full_norm)
    return slug or "unknown"


# Order matters — first match wins.
GROUP_DEFS: tuple[QuestionGroupDef, ...] = (
    QuestionGroupDef(
        "compensation",
        "Compensation (CTC / salary)",
        "Current and expected CTC —  38 LPA current, 45 LPA expected",
        (
            re.compile(r"\bctc\b"),
            re.compile(r"salary expectation"),
            re.compile(r"expected.{0,12}salary"),
            re.compile(r"current.{0,12}expected"),
        ),
    ),
    QuestionGroupDef(
        "join_availability",
        "Join / availability (Yes/No)",
        "Yes only if notice period is 15 days or less — otherwise No",
        (
            re.compile(r"available to join"),
            re.compile(r"join immediately"),
            re.compile(r"within\s*15\s*days"),
        ),
    ),
    QuestionGroupDef(
        "notice_period",
        "Notice period",
        "Days or weeks — e.g. 60 days, or LWD if serving",
        (
            re.compile(r"notice period"),
            re.compile(r"last working day"),
            re.compile(r"serving.{0,20}notice"),
        ),
    ),
    QuestionGroupDef(
        "current_location",
        "Current location",
        "City you are based in — e.g. Bengaluru",
        (
            re.compile(r"current location"),
            re.compile(r"where are you.{0,20}located"),
            re.compile(r"where.{0,10}you located"),
        ),
    ),
    QuestionGroupDef(
        "preferred_location",
        "Preferred location / relocation",
        "Preferred city or Yes/No for relocation",
        (
            re.compile(r"select.{0,30}(city|cities)"),
            re.compile(r"preferred location"),
            re.compile(r"willing to relocate"),
            re.compile(r"open to relocate"),
            re.compile(r"residing in"),
            re.compile(r"currently residing"),
            re.compile(r"currently living in"),
            re.compile(r"ready to relocate"),
        ),
    ),
    QuestionGroupDef(
        "total_experience",
        "Total work experience (years)",
        "Overall IT/industry experience — e.g. 5 years",
        (
            re.compile(r"total.{0,20}work experience"),
            re.compile(r"total experience in (it|the industry|industry)"),
            re.compile(r"total years of experience in it\b"),
            re.compile(r"total experience in it\b"),
            re.compile(r"^total experience\?"),
        ),
    ),
    QuestionGroupDef(
        "pan",
        "PAN number",
        "Your PAN — e.g. ABCDE1234F",
        (re.compile(r"\bpan\b"),),
    ),
    QuestionGroupDef(
        "current_employer",
        "Current / previous employer",
        "Company name — e.g. Decentro Tech",
        (
            re.compile(r"current.{0,12}employer"),
            re.compile(r"previous employer"),
            re.compile(r"current.{0,8}previous employer"),
        ),
    ),
    QuestionGroupDef(
        "uan",
        "UAN (PF)",
        "Yes / No or your UAN number",
        (re.compile(r"\buan\b"),),
    ),
    QuestionGroupDef(
        "reason_for_change",
        "Reason for job change",
        "Brief reason — growth, new challenges, etc.",
        (
            re.compile(r"reason for looking"),
            re.compile(r"new opportunity"),
            re.compile(r"why.{0,20}looking"),
        ),
    ),
    QuestionGroupDef(
        "interview_availability",
        "Interview / test availability",
        "Yes/No or when you are available",
        (
            re.compile(r"coding test"),
            re.compile(r"technical discussion"),
            re.compile(r"available.{0,30}next.{0,10}days"),
        ),
    ),
    QuestionGroupDef(
        "reports_to",
        "Team / reports",
        "People reporting to you — e.g. 0 or 3",
        (
            re.compile(r"report to you"),
            re.compile(r"people report"),
            re.compile(r"team size"),
        ),
    ),
)


def classify_question(label: str) -> str:
    norm = _norm(label)
    if not norm:
        return f"unique:{question_key(label)}"

    for group in GROUP_DEFS:
        for pattern in group.patterns:
            if pattern.search(norm):
                return group.group_id

    if re.search(
        r"(how many|years?.{0,15}experience|experience.{0,20}years?|relevant experience)",
        norm,
    ):
        skill_match = re.search(
            r"experience.{0,30}(?:in|with)\s+([a-z0-9+#./\s&-]+?)(?:\s*\(|years|\?|$)",
            norm,
        )
        if skill_match:
            phrase = skill_match.group(1).strip()
            phrase = re.sub(r"\s+in years$", "", phrase)
            if phrase and phrase not in ("the industry", "it", "software"):
                canonical = _canonical_skill_id(phrase, full_norm=norm)
                return f"skill:{canonical}"

    if re.search(r"do (you|u) have experience", norm) or re.search(
        r"experience.{0,20}\?", norm
    ):
        for pattern, skill_id in _CANONICAL_SKILL_RULES:
            if pattern.search(norm):
                return f"skill_yesno:{skill_id}"

    return f"unique:{question_key(label)}"


def group_title(group_id: str) -> str:
    for group in GROUP_DEFS:
        if group.group_id == group_id:
            return group.title
    if group_id.startswith("skill:"):
        skill = group_id.split(":", 1)[1].replace("_", " ")
        return f"Experience in {skill.title()} (years)"
    if group_id.startswith("skill_yesno:"):
        skill = group_id.split(":", 1)[1].replace("_", " ")
        return f"Experience with {skill.title()} (yes/no + years if asked)"
    if group_id.startswith("unique:"):
        return "Other"
    return group_id.replace("_", " ").title()


def group_hint(group_id: str) -> str:
    for group in GROUP_DEFS:
        if group.group_id == group_id:
            return group.hint
    return ""


@dataclass
class PendingQuestionGroup:
    group_id: str
    title: str
    hint: str
    variants: list[str]
    jobs: list[dict]


def group_pending_entries(entries: list[dict]) -> list[PendingQuestionGroup]:
    by_group: dict[str, PendingQuestionGroup] = {}

    for entry in entries:
        label = str(entry.get("question", "")).strip()
        if not label:
            continue
        gid = classify_question(label)
        if gid not in by_group:
            title = label if gid.startswith("unique:") else group_title(gid)
            by_group[gid] = PendingQuestionGroup(
                group_id=gid,
                title=title,
                hint=group_hint(gid),
                variants=[],
                jobs=[],
            )
        group = by_group[gid]
        if label not in group.variants:
            group.variants.append(label)
        seen_urls = {j.get("url") for j in group.jobs}
        for job in entry.get("jobs", []):
            if job.get("url") not in seen_urls:
                group.jobs.append(job)
                seen_urls.add(job.get("url"))

    groups = list(by_group.values())
    groups.sort(key=lambda g: (-len(g.jobs), -len(g.variants), g.title))
    return groups
