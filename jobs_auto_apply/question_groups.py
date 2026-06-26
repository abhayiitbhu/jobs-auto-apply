from __future__ import annotations

import re
from dataclasses import dataclass

from .question_keys import question_key


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
    (re.compile(r"langchain"), "langchain"),
    (re.compile(r"\bnlp\b|natural language processing"), "nlp"),
    (re.compile(r"computer vision|opencv|image recognition"), "computer_vision"),
    (re.compile(r"\bdsa\b|data structur|\balgorithms?\b"), "dsa"),
    (re.compile(r"pytorch|tensorflow|deep learning"), "deep_learning"),
    (re.compile(r"azure data factory|\badf\b"), "azure_adf"),
    (re.compile(r"\bgcp\b|google cloud|bigquery"), "gcp"),
    (re.compile(r"\bazure\b"), "azure"),
    (re.compile(r"\bglue\b|\bredshift\b"), "glue_redshift"),
    (re.compile(r"\bmysql\b"), "mysql"),
    (re.compile(r"\bmongodb\b|mongo db"), "mongodb"),
    (re.compile(r"\bkafka\b"), "kafka"),
    (re.compile(r"\bredis\b"), "redis"),
    (re.compile(r"\bdocker\b"), "docker"),
    (re.compile(r"kubernetes|\bk8s\b"), "kubernetes"),
    (
        re.compile(
            r"llm|large language|gen\s*ai|genai|gpt|openai|llama|transformer|"
            r"\brag\b|retrieval augmented|\bai\b|artificial intelligence|machine learning|\bml\b"
        ),
        "ai_ml",
    ),
    (re.compile(r"javascript|typescript|node\.?js|nodejs"), "javascript"),
    (re.compile(r"angular(?:\s*js)?"), "angular"),
    (re.compile(r"rest\s*api|restful|\brest\s+ap"), "rest_apis"),
    (re.compile(r"\bjava\b|j2ee|spring|microservices?|jdk|core java"), "java"),
    (re.compile(r"python|fast\s*api|flask|django|pyspark"), "python"),
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
    (re.compile(r"ci\s*/?\s*cd|\bcicd\b|continuous integration"), "ci_cd"),
    (re.compile(r"devops|dev\s*ops|\bsre\b"), "devops"),
    (re.compile(r"ppnr|commercial.{0,24}portfolio|retail.{0,16}wholesale"), "banking_portfolio"),
    (re.compile(r"scripting|automation|terraform|ansible"), "devops_scripting"),
    (re.compile(r"multi.?thread|concurrency"), "concurrency"),
    (re.compile(r"elastic\s*search|elasticsearch"), "elasticsearch"),
    (re.compile(r"databricks"), "databricks"),
    (re.compile(r"snowflake|snow\s*sql|snowpark"), "snowflake"),
    (re.compile(r"\bdbt\b|data build tool"), "dbt"),
    (re.compile(r"airflow|argo|oozie"), "airflow"),
    (re.compile(r"\betl\b|data pipeline|data warehouse"), "etl"),
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
            re.compile(r"\blpa\b"),
            re.compile(r"\bectc\b"),
            re.compile(r"\bcctc\b"),
            re.compile(r"salary expectation"),
            re.compile(r"expected.{0,12}salary"),
            re.compile(r"current.{0,12}expected"),
            re.compile(r"annual salary"),
            re.compile(r"current salary"),
            re.compile(r"gross monthly salary"),
            re.compile(r"take.?home salary"),
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
        "last_working_day",
        "Last working day (LWD)",
        "Date as DD/MM/YYYY — e.g. 12/06/2026 from application_facts",
        (
            re.compile(r"last working da(?:y|te)"),
            re.compile(r"\blwd\b"),
            re.compile(r"last day of (?:work|working|employment)"),
            re.compile(r"(?:date of |)relieving date"),
            re.compile(r"date of relieving"),
        ),
    ),
    QuestionGroupDef(
        "f2f_interview",
        "Face-to-face interview availability",
        "No — not available for in-person / final F2F rounds",
        (
            re.compile(r"\bf2f\b"),
            re.compile(r"face[\s-]?to[\s-]?face"),
            re.compile(r"final.{0,24}(f2f|face)"),
            re.compile(r"(f2f|face).{0,24}final"),
        ),
    ),
    QuestionGroupDef(
        "notice_period",
        "Notice period",
        "0 days — immediately available",
        (
            re.compile(r"notice period"),
            re.compile(r"serving.{0,20}notice"),
        ),
    ),
    QuestionGroupDef(
        "current_location",
        "Current location",
        "City you are based in — e.g. Bengaluru",
        (
            re.compile(r"current location"),
            re.compile(r"current.{0,16}native.{0,16}location"),
            re.compile(r"native location"),
            re.compile(r"what is your location"),
            re.compile(r"enter your.{0,20}location"),
            re.compile(r"^location$"),
            re.compile(r"^city$"),
            re.compile(r"your city"),
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
            re.compile(r"preferred.{0,8}location"),
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
        "pincode",
        "Postal pincode",
        "6-digit Indian pincode for your current address — e.g. 560001",
        (
            re.compile(r"\bpin\s*code\b"),
            re.compile(r"\bpincode\b"),
            re.compile(r"\bzip\s*code\b"),
            re.compile(r"\bpostal\s*code\b"),
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
        "prior_application",
        "Previously applied / interviewed",
        "No — unless you have already applied or interviewed for this employer",
        (
            re.compile(r"profile previously uploaded"),
            re.compile(r"interview attended"),
            re.compile(r"can not process"),
            re.compile(r"cannot process"),
            re.compile(r"applied before"),
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

    if re.search(r"do (you|u) have experience", norm) or (
        re.search(r"experience.{0,20}\?", norm) and not re.search(r"\byears?\b", norm)
    ):
        for pattern, skill_id in _CANONICAL_SKILL_RULES:
            if pattern.search(norm):
                return f"skill_yesno:{skill_id}"

    if re.search(r"^experience in\b", norm):
        phrase = re.sub(r"^experience in\s+", "", norm).strip()
        phrase = re.split(r"\?", phrase)[0].strip()
        if phrase:
            canonical = _canonical_skill_id(phrase, full_norm=norm)
            return f"skill:{canonical}"

    if re.search(r"total experience in\b", norm):
        phrase = re.sub(r"total experience in\s+", "", norm).strip()
        phrase = re.split(r"\?", phrase)[0].strip()
        if phrase:
            canonical = _canonical_skill_id(phrase, full_norm=norm)
            return f"skill:{canonical}"

    if re.search(r"\bai domains?\b", norm):
        return "skill:ai_ml_domains"

    if re.search(r"hyperscaler|knowledge of.{0,40}(azure|aws|gcp)", norm):
        return "skill:cloud_ai"

    if re.search(
        r"(how many|years?.{0,15}experience|experience.{0,20}years?|relevant experience)",
        norm,
    ):
        skill_match = re.search(
            r"experience.{0,40}(?:in|with|developing|deploying)\s+([a-z0-9+#./\s&-]+?)(?:\s*\(|years|\?|$)",
            norm,
        )
        if not skill_match and re.search(r"backend", norm):
            return "skill:backend"
        if skill_match:
            phrase = skill_match.group(1).strip()
            phrase = re.sub(r"\s+in years$", "", phrase)
            if phrase and phrase not in ("the industry", "it", "software"):
                canonical = _canonical_skill_id(phrase, full_norm=norm)
                return f"skill:{canonical}"

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
