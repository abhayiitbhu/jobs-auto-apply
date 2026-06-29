from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dataclasses_jsonschema import JsonSchemaMixin


@dataclass
class UserConfig(JsonSchemaMixin):
    name: str
    email: str
    phone: str
    linkedin: str
    expected_display_name: str
    github: str = ""
    # Dialing code for the candidate's phone (e.g. "+91", "+1"). Used only to format
    # the phone number in cover-letter signatures. Leave blank to print the number
    # exactly as entered (no country code assumed).
    phone_country_code: str = ""


@dataclass
class CompensationConfig(JsonSchemaMixin):
    current_ctc_lpa: float = 0.0
    current_fixed_lpa: float = 0.0
    current_variable_lpa: float = 0.0
    current_esops_lpa: float = 0.0
    expected_ctc_lpa: float = 0.0


@dataclass
class ProfileConfig(JsonSchemaMixin):
    headline: str = ""
    years_experience: int = 0
    core_skills: list[str] = field(default_factory=list)
    target_roles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    skip_companies: list[str] = field(default_factory=list)
    # Role filters are fully config-driven and domain-neutral so the tool works for
    # any field (law, finance, design, engineering, …). Define what to skip via
    # skip_role_keywords / skip_role_patterns and what to keep via keep_role_keywords.
    skip_role_keywords: list[str] = field(default_factory=list)
    # "Keep" anchors — a title matching any of these is kept even if it also matches a
    # skip rule. Empty by default; set to the terms that define your target roles
    # (e.g. "attorney", "counsel" for law; "backend", "platform" for engineering).
    keep_role_keywords: list[str] = field(default_factory=list)
    # Extra raw regex patterns to skip (full power, matched against the title,
    # case-insensitive). Use for anything the keyword/built-in groups can't express.
    skip_role_patterns: list[str] = field(default_factory=list)
    # Skills/domains you have NO experience in. A job is skipped when its TITLE is
    # about one of these AND does not also name a skill you do have (core_skills /
    # skill_years > 0). Empty by default; populate it for your own field.
    skip_no_experience_skills: list[str] = field(default_factory=list)


@dataclass
class CoverLetterConfig(JsonSchemaMixin):
    mode: str = "dynamic"  # dynamic | template
    include_ctc: bool = True
    max_words: int = 200
    reference_path: str = "profile/cover_letter_reference.txt"


@dataclass
class ResumeConfig(JsonSchemaMixin):
    path: str
    sync_to_wellfound: bool = False
    sync_to_naukri: bool = True
    naukri_sync_interval_minutes: int = 30
    sync_to_hirist: bool = True
    hirist_sync_interval_minutes: int = 30


@dataclass
class WellfoundFiltersConfig(JsonSchemaMixin):
    use_profile_filters: bool = True
    roles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    remote_policy: str = "some"
    experience_levels: list[str] = field(default_factory=list)
    job_types: list[str] = field(default_factory=list)
    keywords: str = ""
    skills: list[str] = field(default_factory=list)
    salary_min: int | None = None
    salary_currency: str = "INR"
    include_no_salary: bool = True
    recently_active: str | None = "week"
    sort: str = "newest"


@dataclass
class UplersFiltersConfig(JsonSchemaMixin):
    keywords: str = ""
    skills: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    experience_years_min: int | None = 3
    remote_only: bool = False


@dataclass
class NaukriFiltersConfig(JsonSchemaMixin):
    keywords: str = ""  # search keywords; set to your role/skills in config.yaml
    locations: list[str] = field(default_factory=list)  # empty = India-wide (no city in URL)
    experience_min: int | None = 3
    experience_max: int | None = 8
    salary_min_lakhs: float | None = None
    remote_only: bool = False
    quick_apply_only: bool = True
    sort: str = "freshness"  # freshness | date | newest | relevance
    max_job_age_days: int | None = None  # e.g. 7 → "Last 7 days" in Freshness filter
    max_pages: int = 1  # scroll batches on single Aurus SRP (scroll → collect → apply)


@dataclass
class HiristFiltersConfig(JsonSchemaMixin):
    search_urls: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)  # set your role/skill keywords in config.yaml
    cities: list[str] = field(default_factory=list)  # empty = all India
    experience: str = "2-5"  # 0-2, 2-5, 5-10, 10+ when using keyword search UI
    experience_min: int | None = None  # minexp query param when building URLs
    experience_max: int | None = None  # maxexp query param when building URLs
    max_pages: int = 1  # SRP pages per search feed (apply page 1, then page 2, …)


@dataclass
class InstahyreFiltersConfig(JsonSchemaMixin):
    search_urls: list[str] = field(default_factory=list)
    feeds: list[dict[str, Any]] = field(default_factory=list)
    # Job functions to target (human names like "Backend Development", or raw
    # Instahyre "/api/v1/job_function/<id>" paths). Empty = use the built-in
    # fallback in instahyre/feeds.py.
    job_functions: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    experience_years: int | None = None
    company_size: str = "All"  # Small, Medium, Large, All
    # --- Instahyre platform mappings (override/extend the built-in defaults so the
    # tool works for any role family, not just backend/software) ---
    # Fallback skills string used when a search feed doesn't specify its own skills.
    default_skills: str = ""
    # Map a human job-function name (case-insensitive) to its Instahyre API path,
    # e.g. {"data science": "/api/v1/job_function/12"}. Merged over the built-ins.
    job_function_aliases: dict[str, str] = field(default_factory=dict)
    # Map a skill keyword (lowercase) to the Instahyre selectize chip data-value,
    # e.g. {"node": "Nodejs", "golang": "Go"}. Merged over the built-ins.
    skill_chip_values: dict[str, str] = field(default_factory=dict)
    # Map a chip data-value to the text to type to surface it in the dropdown,
    # e.g. {"Nodejs": "nodejs"}. Merged over the built-ins.
    skill_type_queries: dict[str, str] = field(default_factory=dict)


@dataclass
class PlatformConfig(JsonSchemaMixin):
    enabled: bool
    cookies_file: str
    filters: (
        WellfoundFiltersConfig
        | UplersFiltersConfig
        | NaukriFiltersConfig
        | HiristFiltersConfig
        | InstahyreFiltersConfig
    )


@dataclass
class LLMConfig(JsonSchemaMixin):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:11434"
    model: str = "job-answers"
    verifier_model: str = "job-verify"
    verifier_enabled: bool = True
    temperature: float = 0.05
    max_tokens: int = 256
    keep_alive: str = "30m"  # keep the model resident between calls (avoids reloads)
    max_concurrency: int = 2  # in-flight local Ollama calls (1 if RAM-constrained)
    auto_save: bool = True
    auto_answer_pending: bool = True
    retry_pending_jobs: bool = True
    prompt_pending_questions: bool = False
    min_confidence: float = 0.92
    min_confidence_rag_agree: float = 0.88
    vector_agree_score: float = 0.80  # similarity floor for vector+LLM agreement
    min_confidence_new_experience: float = 0.96
    min_confidence_persist: float = 0.98
    plain_text_confidence: float = 0.35
    # Cap applied to an LLM answer's effective confidence when NO independent source
    # (RAG rule, similar past answer, or verifier) corroborates it. The raw number a
    # small model self-reports is unreliable, so an uncorroborated answer is still
    # used for the current fill but capped below the persist threshold so it is not
    # written to memory on the model's word alone.
    uncorroborated_confidence_cap: float = 0.6
    rag_agree_input_types: list[str] = field(
        default_factory=lambda: [
            "single_choice",
            "yes_no_checkbox",
            "ctc_numeric",
            "years_numeric",
            "number",
            "pincode",
            "date",
        ]
    )
    use_faiss_memory: bool = True
    rag_top_k: int = 3
    # Minimum composite similarity score a retrieved prior answer must reach to be
    # returned by retrieve_similar_answers. 0.0 disables the cutoff (return all top-k).
    rag_min_score: float = 0.5
    vector_auto_answer_score: float = 0.92
    embeddings_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    faiss_index_dir: str = "data/faiss"


@dataclass
class PlatformDelaysConfig(JsonSchemaMixin):
    """Fixed UI waits per platform (ms). Lower = faster; raise if clicks miss."""

    instahyre_ms: int = 200
    instahyre_advance_ms: int = 200
    hirist_step_ms: int = 300
    naukri_chatbot_step_ms: int = 400
    naukri_chip_poll_ms: int = 120


@dataclass
class ApplicationConfig(JsonSchemaMixin):
    jobs_per_platform: int = 0
    max_jobs_per_run: int = 0
    require_review: bool = False
    review_dir: str = "data/review"
    delay_seconds_min: int = 0
    delay_seconds_max: int = 0
    apply_workers: int = 10
    naukri_apply_workers: int = 10
    hirist_apply_workers: int = 10
    instahyre_apply_workers: int = 5  # parallel Instahyre tabs (collect+apply path)
    parallel_platforms: bool = False  # run naukri + hirist + instahyre concurrently (separate browsers)
    skip_external_ats: bool = True
    dry_run: bool = False
    follow_external_from_wellfound: bool = False
    skip_location_blocked: bool = True
    skip_ineligible_salary: bool = True
    min_inr_salary_lpa: float = 25.0
    apply_retries: int = 1
    retry_backoff_ms: int = 1500
    interactive_questions: bool = True
    confirm_new_answers: bool = True
    rag_answer_questions: bool = True
    one_job_per_company: bool = True
    enrich_workers: int = 4
    pipeline_apply: bool = True
    platform_delays: PlatformDelaysConfig = field(default_factory=PlatformDelaysConfig)


@dataclass
class BrowserConfig(JsonSchemaMixin):
    headless: bool = False
    slow_mo_ms: int = 100
    # Reuse your installed Chrome profile (already signed into Google / Wellfound / Uplers)
    use_chrome_profile: bool = True
    chrome_user_data_dir: str = ""  # empty = auto-detect OS default Chrome folder
    chrome_profile_name: str = "Default"  # Default, Profile 1, Profile 2, ...
    chrome_channel: str = "chrome"  # use system Google Chrome (not bundled Chromium)


@dataclass
class AuthConfig(JsonSchemaMixin):
    # browser = sign in with Google once in a real browser (passkey/2FA works); session is saved
    # cookies = legacy manual cookie export
    method: str = "browser"
    sessions_dir: str = "data/sessions"
    login_timeout_seconds: int = 300


@dataclass
class WhatsAppConfig(JsonSchemaMixin):
    """Send pending questions to WhatsApp Web and read your replies.

    Uses an unofficial Playwright-driven WhatsApp Web session (no server/tunnel).
    Link once via QR (python main.py whatsapp-login); the session persists in
    profile_dir. Note: automating WhatsApp Web is against WhatsApp's ToS — use
    at your own risk with a number you're willing to expose to that risk.
    """

    enabled: bool = False
    # inline   = ask questions over WhatsApp at the end of each run (run holds the session)
    # listener = a separate `whatsapp-listen` daemon owns the session; runs just defer
    #            questions and the always-on listener asks + retries as replies arrive.
    mode: str = "inline"
    phone: str = ""  # destination number incl. country code, e.g. 919876543210 (your own = message yourself)
    profile_dir: str = "data/whatsapp_profile"
    headless: bool = False
    reply_timeout_seconds: int = 900  # how long to wait for a reply per question
    poll_interval_seconds: int = 5
    login_timeout_seconds: int = 180  # time to scan the QR on first link
    listen_idle_seconds: int = 20  # how often the listener re-checks for new pending questions
    skip_keyword: str = "skip"  # reply this to skip a question
    drop_keyword: str = "drop"  # reply this to abandon the job(s)
    ignore_keyword: str = "ignore"  # reply this to mark question N/A forever


@dataclass
class TelegramConfig(JsonSchemaMixin):
    """Send pending questions to a Telegram bot and read your replies.

    Official, free Bot API via long polling — no server, webhook, or tunnel, and
    no ToS/ban risk. Create a bot with @BotFather, paste the token here, then run
    `python main.py telegram-login` and send /start to your bot once.
    """

    enabled: bool = False
    # inline   = ask questions at the end of each run (run does the polling)
    # listener = `serve` runs Telegram in-process; or run `telegram-listen` standalone
    mode: str = "inline"
    bot_token: str = ""
    chat_id: str = ""  # auto-captured by telegram-login if left blank
    reply_timeout_seconds: int = 900
    listen_idle_seconds: int = 20
    skip_keyword: str = "skip"
    drop_keyword: str = "drop"
    ignore_keyword: str = "ignore"


@dataclass
class PathsConfig(JsonSchemaMixin):
    application_facts: str = "profile/application_facts.yaml"
    user_memory: str = "data/user_memory.json"
    pending_questions: str = "data/pending_questions.json"
    naukri_resume_sync: str = "data/naukri_resume_sync.json"
    hirist_resume_sync: str = "data/hirist_resume_sync.json"
    # User-specific Q&A corrections used by scripts/cleanup_user_memory.py.
    memory_corrections: str = "data/memory_corrections.json"


@dataclass
class AnswersPolicyConfig(JsonSchemaMixin):
    notice_join_threshold_days: int = 15
    default_year_chip_options: list[str] = field(
        default_factory=lambda: [
            "No experience",
            "<6 years",
            "6-8 years",
            "8+ years",
        ]
    )


@dataclass
class StateConfig(JsonSchemaMixin):
    applied_jobs_file: str = "data/applied_jobs.json"
    log_file: str = "data/run.log"


@dataclass
class WorkdayAddressConfig(JsonSchemaMixin):
    line1: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""


@dataclass
class WorkdayConfig(JsonSchemaMixin):
    password: str = ""  # used for sign-in / create-account on Workday career sites
    how_did_you_hear: str = "LinkedIn"
    skip_voluntary_disclosures: bool = True
    max_form_pages: int = 10
    address: WorkdayAddressConfig = field(default_factory=WorkdayAddressConfig)


@dataclass
class AppConfig(JsonSchemaMixin):
    user: UserConfig
    resume: ResumeConfig
    profile: ProfileConfig
    compensation: CompensationConfig
    cover_letter: CoverLetterConfig
    cover_note: str
    wellfound: PlatformConfig
    uplers: PlatformConfig
    naukri: PlatformConfig
    hirist: PlatformConfig
    instahyre: PlatformConfig
    workday: WorkdayConfig
    llm: LLMConfig
    whatsapp: WhatsAppConfig
    telegram: TelegramConfig
    application: ApplicationConfig
    browser: BrowserConfig
    auth: AuthConfig
    state: StateConfig
    paths: PathsConfig
    answers: AnswersPolicyConfig

    def __post_init__(self) -> None:
        # base_dir is not part of the YAML config, it's derived from the config file path
        self.base_dir: Path = Path.cwd()

    @property
    def resume_path(self) -> Path:
        return self.base_dir / self.resume.path

    @property
    def cover_letter_reference_path(self) -> Path:
        return self.base_dir / self.cover_letter.reference_path

    @property
    def applied_jobs_path(self) -> Path:
        return self.base_dir / self.state.applied_jobs_file

    @property
    def log_path(self) -> Path:
        return self.base_dir / self.state.log_file

    @property
    def user_memory_path(self) -> Path:
        return self.base_dir / self.paths.user_memory

    @property
    def pending_questions_path(self) -> Path:
        return self.base_dir / self.paths.pending_questions

    @property
    def naukri_resume_sync_path(self) -> Path:
        return self.base_dir / self.paths.naukri_resume_sync

    @property
    def hirist_resume_sync_path(self) -> Path:
        return self.base_dir / self.paths.hirist_resume_sync

    @property
    def memory_corrections_path(self) -> Path:
        return self.base_dir / self.paths.memory_corrections

    @property
    def whatsapp_profile_path(self) -> Path:
        p = self.base_dir / self.whatsapp.profile_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def drop_keywords_path(self) -> Path:
        # User-managed title blocklist (e.g. added by replying "drop <keyword>"
        # over Telegram). Merged into profile.skip_role_keywords at load time.
        return self.base_dir / "data" / "drop_keywords.json"

    @property
    def telegram_chat_path(self) -> Path:
        return self.base_dir / "data" / "telegram_chat.json"

    @property
    def telegram_offset_path(self) -> Path:
        # Persist the getUpdates offset so a `serve --reload` restart resumes
        # from the last processed update instead of skipping past replies that
        # arrived while the worker was reloading.
        return self.base_dir / "data" / "telegram_offset.json"

    @property
    def auth_sessions_dir(self) -> Path:
        p = self.base_dir / self.auth.sessions_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def sessions_dir(self) -> Path:
        """Alias used by browser session manager."""
        return self.auth_sessions_dir

    def cookies_path(self, platform: str) -> Path:
        mapping = {
            "wellfound": self.wellfound.cookies_file,
            "uplers": self.uplers.cookies_file,
            "naukri": self.naukri.cookies_file,
            "hirist": self.hirist.cookies_file,
            "instahyre": self.instahyre.cookies_file,
        }
        if platform not in mapping:
            raise ValueError(f"Unknown platform: {platform}")
        return self.base_dir / mapping[platform]


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    return value if isinstance(value, dict) else {}


def _user_config(data: dict[str, Any]) -> UserConfig:
    name = str(data.get("name", "")).strip()
    # Default the Wellfound nav display name to the first name when not provided.
    display = str(data.get("expected_display_name", "")).strip()
    if not display and name:
        display = name.split()[0]
    return UserConfig(
        name=name,
        email=str(data.get("email", "")),
        phone=str(data.get("phone", "")),
        linkedin=str(data.get("linkedin", "")),
        expected_display_name=display,
        github=str(data.get("github", "")),
        phone_country_code=str(data.get("phone_country_code", "")).strip(),
    )


def _wellfound_filters(data: dict[str, Any]) -> WellfoundFiltersConfig:
    return WellfoundFiltersConfig(
        use_profile_filters=bool(data.get("use_profile_filters", True)),
        roles=list(data.get("roles", [])),
        locations=list(data.get("locations", [])),
        remote_policy=str(data.get("remote_policy", "some")),
        experience_levels=list(data.get("experience_levels", [])),
        job_types=list(data.get("job_types", [])),
        keywords=str(data.get("keywords", "")),
        skills=list(data.get("skills", [])),
        salary_min=data.get("salary_min"),
        salary_currency=str(data.get("salary_currency", "INR")),
        include_no_salary=bool(data.get("include_no_salary", True)),
        recently_active=data.get("recently_active"),
        sort=str(data.get("sort", "newest")),
    )


def _uplers_filters(data: dict[str, Any]) -> UplersFiltersConfig:
    return UplersFiltersConfig(
        keywords=str(data.get("keywords", "")),
        skills=list(data.get("skills", [])),
        locations=list(data.get("locations", [])),
        roles=list(data.get("roles", [])),
        experience_years_min=data.get("experience_years_min", 3),
        remote_only=bool(data.get("remote_only", False)),
    )


def _naukri_filters(data: dict[str, Any]) -> NaukriFiltersConfig:
    loc = data.get("locations")
    return NaukriFiltersConfig(
        keywords=str(data.get("keywords", "")),
        locations=list(loc) if isinstance(loc, list) else [],
        experience_min=data.get("experience_min", 3),
        experience_max=data.get("experience_max"),
        salary_min_lakhs=data.get("salary_min_lakhs"),
        remote_only=bool(data.get("remote_only", False)),
        quick_apply_only=bool(data.get("quick_apply_only", True)),
        sort=str(data.get("sort", "freshness")),
        max_job_age_days=data.get("max_job_age_days"),
        max_pages=int(data.get("max_pages", 1)),
    )


def _hirist_filters(data: dict[str, Any]) -> HiristFiltersConfig:
    kw = data.get("keywords", [])
    urls = data.get("search_urls", [])
    cities = data.get("cities")
    return HiristFiltersConfig(
        search_urls=list(urls) if isinstance(urls, list) else [str(urls)] if urls else [],
        keywords=list(kw) if isinstance(kw, list) else [str(kw)] if kw else [],
        cities=list(cities) if isinstance(cities, list) else [],
        experience=str(data.get("experience", "2-5")),
        experience_min=data.get("experience_min"),
        experience_max=data.get("experience_max"),
        max_pages=int(data.get("max_pages", 1)),
    )


def _platform_delays(data: dict[str, Any]) -> PlatformDelaysConfig:
    raw = data if isinstance(data, dict) else {}
    return PlatformDelaysConfig(
        instahyre_ms=int(raw.get("instahyre_ms", 200)),
        instahyre_advance_ms=int(raw.get("instahyre_advance_ms", 200)),
        hirist_step_ms=int(raw.get("hirist_step_ms", 300)),
        naukri_chatbot_step_ms=int(raw.get("naukri_chatbot_step_ms", 400)),
        naukri_chip_poll_ms=int(raw.get("naukri_chip_poll_ms", 120)),
    )


def _str_str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _instahyre_filters(data: dict[str, Any]) -> InstahyreFiltersConfig:
    urls = data.get("search_urls", [])
    raw_feeds = data.get("feeds", [])
    feeds = [f for f in raw_feeds if isinstance(f, dict)] if isinstance(raw_feeds, list) else []
    return InstahyreFiltersConfig(
        search_urls=list(urls) if isinstance(urls, list) else [str(urls)] if urls else [],
        feeds=feeds,
        job_functions=list(data.get("job_functions", [])),
        locations=list(data.get("locations", [])),
        experience_years=data.get("experience_years"),
        company_size=str(data.get("company_size", "All")),
        default_skills=str(data.get("default_skills", "")),
        job_function_aliases=_str_str_map(data.get("job_function_aliases")),
        skill_chip_values=_str_str_map(data.get("skill_chip_values")),
        skill_type_queries=_str_str_map(data.get("skill_type_queries")),
    )


def _platform_config(
    raw: dict[str, Any],
    key: str,
    *,
    default_cookies: str,
    filter_fn,
    enabled_default: bool = True,
) -> PlatformConfig:
    section = _section(raw, key)
    if not section:
        return PlatformConfig(enabled=False, cookies_file=default_cookies, filters=filter_fn({}))
    return PlatformConfig(
        enabled=bool(section.get("enabled", enabled_default)),
        cookies_file=str(section.get("cookies_file", default_cookies)),
        filters=filter_fn(_section(section, "filters") or section),
    )


def _profile_config(data: dict[str, Any]) -> ProfileConfig:
    return ProfileConfig(
        headline=str(data.get("headline", "")),
        years_experience=int(data.get("years_experience", 0)),
        core_skills=list(data.get("core_skills", [])),
        target_roles=list(data.get("target_roles", [])),
        locations=list(data.get("locations", [])),
        skip_companies=list(data.get("skip_companies", [])),
        skip_role_keywords=list(data.get("skip_role_keywords", [])),
        keep_role_keywords=list(data.get("keep_role_keywords", [])),
        skip_role_patterns=list(data.get("skip_role_patterns", [])),
        skip_no_experience_skills=list(data.get("skip_no_experience_skills", [])),
    )


def _compensation_config(data: dict[str, Any]) -> CompensationConfig:
    return CompensationConfig(
        current_ctc_lpa=float(data.get("current_ctc_lpa", 0)),
        current_fixed_lpa=float(data.get("current_fixed_lpa", 0)),
        current_variable_lpa=float(data.get("current_variable_lpa", 0)),
        current_esops_lpa=float(data.get("current_esops_lpa", 0)),
        expected_ctc_lpa=float(data.get("expected_ctc_lpa", 0)),
    )


def _cover_letter_config(data: dict[str, Any]) -> CoverLetterConfig:
    return CoverLetterConfig(
        mode=str(data.get("mode", "dynamic")),
        include_ctc=bool(data.get("include_ctc", True)),
        max_words=int(data.get("max_words", 200)),
        reference_path=str(data.get("reference_path", "profile/cover_letter_reference.txt")),
    )


def _workday_config(data: dict[str, Any]) -> WorkdayConfig:
    addr = _section(data, "address")
    return WorkdayConfig(
        password=str(data.get("password", "")),
        how_did_you_hear=str(data.get("how_did_you_hear", "LinkedIn")),
        skip_voluntary_disclosures=bool(data.get("skip_voluntary_disclosures", True)),
        max_form_pages=int(data.get("max_form_pages", 10)),
        address=WorkdayAddressConfig(
            line1=str(addr.get("line1", "")),
            city=str(addr.get("city", "")),
            state=str(addr.get("state", "")),
            postal_code=str(addr.get("postal_code", "")),
            country=str(addr.get("country", "")),
        ),
    )


def _llm_config(data: dict[str, Any]) -> LLMConfig:
    return LLMConfig(
        enabled=bool(data.get("enabled", False)),
        base_url=str(data.get("base_url", "http://127.0.0.1:11434")).strip(),
        model=str(data.get("model", "job-answers")),
        verifier_model=str(data.get("verifier_model", "job-verify")),
        verifier_enabled=bool(data.get("verifier_enabled", True)),
        temperature=float(data.get("temperature", 0.05)),
        max_tokens=int(data.get("max_tokens", 256)),
        keep_alive=str(data.get("keep_alive", "30m")),
        max_concurrency=int(data.get("max_concurrency", 2)),
        auto_save=bool(data.get("auto_save", True)),
        auto_answer_pending=bool(data.get("auto_answer_pending", True)),
        retry_pending_jobs=bool(data.get("retry_pending_jobs", True)),
        prompt_pending_questions=bool(data.get("prompt_pending_questions", False)),
        min_confidence=float(data.get("min_confidence", 0.92)),
        min_confidence_rag_agree=float(data.get("min_confidence_rag_agree", 0.88)),
        vector_agree_score=float(data.get("vector_agree_score", 0.80)),
        min_confidence_new_experience=float(data.get("min_confidence_new_experience", 0.96)),
        min_confidence_persist=float(data.get("min_confidence_persist", 0.98)),
        plain_text_confidence=float(data.get("plain_text_confidence", 0.35)),
        uncorroborated_confidence_cap=float(data.get("uncorroborated_confidence_cap", 0.6)),
        rag_agree_input_types=list(
            data.get(
                "rag_agree_input_types",
                [
                    "single_choice",
                    "yes_no_checkbox",
                    "ctc_numeric",
                    "years_numeric",
                    "number",
                    "pincode",
                    "date",
                ],
            )
        ),
        use_faiss_memory=bool(data.get("use_faiss_memory", True)),
        rag_top_k=int(data.get("rag_top_k", 3)),
        rag_min_score=float(data.get("rag_min_score", 0.0)),
        vector_auto_answer_score=float(data.get("vector_auto_answer_score", 0.92)),
        embeddings_model=str(data.get("embeddings_model", "sentence-transformers/all-MiniLM-L6-v2")),
        faiss_index_dir=str(data.get("faiss_index_dir", "data/faiss")),
    )


def _whatsapp_config(data: dict[str, Any]) -> WhatsAppConfig:
    return WhatsAppConfig(
        enabled=bool(data.get("enabled", False)),
        mode=str(data.get("mode", "inline")).strip().lower(),
        phone=re.sub(r"\D", "", str(data.get("phone", ""))),
        profile_dir=str(data.get("profile_dir", "data/whatsapp_profile")),
        headless=bool(data.get("headless", False)),
        reply_timeout_seconds=int(data.get("reply_timeout_seconds", 900)),
        poll_interval_seconds=int(data.get("poll_interval_seconds", 5)),
        login_timeout_seconds=int(data.get("login_timeout_seconds", 180)),
        listen_idle_seconds=int(data.get("listen_idle_seconds", 20)),
        skip_keyword=str(data.get("skip_keyword", "skip")),
        drop_keyword=str(data.get("drop_keyword", "drop")),
        ignore_keyword=str(data.get("ignore_keyword", "ignore")),
    )


def _telegram_config(data: dict[str, Any]) -> TelegramConfig:
    return TelegramConfig(
        enabled=bool(data.get("enabled", False)),
        mode=str(data.get("mode", "inline")).strip().lower(),
        bot_token=str(data.get("bot_token", "")).strip(),
        chat_id=str(data.get("chat_id", "")).strip(),
        reply_timeout_seconds=int(data.get("reply_timeout_seconds", 900)),
        listen_idle_seconds=int(data.get("listen_idle_seconds", 20)),
        skip_keyword=str(data.get("skip_keyword", "skip")),
        drop_keyword=str(data.get("drop_keyword", "drop")),
        ignore_keyword=str(data.get("ignore_keyword", "ignore")),
    )


def load_config(path: Path) -> AppConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid config file: {path}")

    base_dir = path.parent.resolve()
    user = _section(raw, "user")
    resume = _section(raw, "resume")
    application = _section(raw, "application")
    browser = _section(raw, "browser")
    auth = _section(raw, "auth")
    state = _section(raw, "state")
    workday = _workday_config(_section(raw, "workday"))
    profile = _profile_config(_section(raw, "profile"))
    compensation = _compensation_config(_section(raw, "compensation"))
    cover_letter = _cover_letter_config(_section(raw, "cover_letter"))
    llm = _llm_config(_section(raw, "llm"))
    whatsapp = _whatsapp_config(_section(raw, "whatsapp"))
    telegram = _telegram_config(_section(raw, "telegram"))
    paths_raw = _section(raw, "paths")
    answers_raw = _section(raw, "answers")
    paths = PathsConfig(
        application_facts=str(paths_raw.get("application_facts", "profile/application_facts.yaml")),
        user_memory=str(paths_raw.get("user_memory", "data/user_memory.json")),
        pending_questions=str(paths_raw.get("pending_questions", "data/pending_questions.json")),
        naukri_resume_sync=str(paths_raw.get("naukri_resume_sync", "data/naukri_resume_sync.json")),
        hirist_resume_sync=str(paths_raw.get("hirist_resume_sync", "data/hirist_resume_sync.json")),
        memory_corrections=str(paths_raw.get("memory_corrections", "data/memory_corrections.json")),
    )
    answers_policy = AnswersPolicyConfig(
        notice_join_threshold_days=int(answers_raw.get("notice_join_threshold_days", 15)),
        default_year_chip_options=list(
            answers_raw.get(
                "default_year_chip_options",
                ["No experience", "<6 years", "6-8 years", "8+ years"],
            )
        ),
    )

    # Legacy flat config (wellfound-only)
    if "wellfound" not in raw and "cookies" in raw:
        legacy_cookies = str(_section(raw, "cookies").get("file", "cookies.json"))
        wellfound = PlatformConfig(
            enabled=True,
            cookies_file=legacy_cookies,
            filters=_wellfound_filters(_section(raw, "filters")),
        )
        uplers = _platform_config(
            raw, "uplers", default_cookies="cookies.uplers.json", filter_fn=_uplers_filters, enabled_default=False
        )
        naukri = _platform_config(
            raw, "naukri", default_cookies="cookies.naukri.json", filter_fn=_naukri_filters, enabled_default=False
        )
        hirist = _platform_config(
            raw, "hirist", default_cookies="cookies.hirist.json", filter_fn=_hirist_filters, enabled_default=False
        )
        instahyre = _platform_config(
            raw,
            "instahyre",
            default_cookies="cookies.instahyre.json",
            filter_fn=_instahyre_filters,
            enabled_default=False,
        )
    else:
        wellfound = _platform_config(
            raw, "wellfound", default_cookies="cookies.wellfound.json", filter_fn=_wellfound_filters
        )
        uplers = _platform_config(raw, "uplers", default_cookies="cookies.uplers.json", filter_fn=_uplers_filters)
        naukri = _platform_config(raw, "naukri", default_cookies="cookies.naukri.json", filter_fn=_naukri_filters)
        hirist = _platform_config(raw, "hirist", default_cookies="cookies.hirist.json", filter_fn=_hirist_filters)
        instahyre = _platform_config(
            raw, "instahyre", default_cookies="cookies.instahyre.json", filter_fn=_instahyre_filters
        )

    config = AppConfig(
        user=_user_config(user),
        resume=ResumeConfig(
            path=str(resume.get("path", "resume.pdf")),
            sync_to_wellfound=bool(resume.get("sync_to_wellfound", False)),
            sync_to_naukri=bool(resume.get("sync_to_naukri", True)),
            naukri_sync_interval_minutes=int(resume.get("naukri_sync_interval_minutes", 30)),
            sync_to_hirist=bool(resume.get("sync_to_hirist", True)),
            hirist_sync_interval_minutes=int(resume.get("hirist_sync_interval_minutes", 30)),
        ),
        profile=profile,
        compensation=compensation,
        cover_letter=cover_letter,
        cover_note=str(raw.get("cover_note", "")).strip(),
        wellfound=wellfound,
        uplers=uplers,
        naukri=naukri,
        hirist=hirist,
        instahyre=instahyre,
        workday=workday,
        llm=llm,
        whatsapp=whatsapp,
        telegram=telegram,
        application=ApplicationConfig(
            jobs_per_platform=int(application.get("jobs_per_platform", 0)),
            max_jobs_per_run=int(application.get("max_jobs_per_run", application.get("jobs_per_platform", 0))),
            require_review=bool(application.get("require_review", False)),
            review_dir=str(application.get("review_dir", "data/review")),
            delay_seconds_min=int(application.get("delay_seconds_min", 0)),
            delay_seconds_max=int(application.get("delay_seconds_max", 0)),
            apply_workers=int(application.get("apply_workers", 10)),
            naukri_apply_workers=int(application.get("naukri_apply_workers", 10)),
            hirist_apply_workers=int(application.get("hirist_apply_workers", 10)),
            instahyre_apply_workers=int(application.get("instahyre_apply_workers", 5)),
            parallel_platforms=bool(application.get("parallel_platforms", False)),
            skip_external_ats=bool(application.get("skip_external_ats", True)),
            dry_run=bool(application.get("dry_run", False)),
            follow_external_from_wellfound=bool(application.get("follow_external_from_wellfound", False)),
            skip_location_blocked=bool(application.get("skip_location_blocked", True)),
            skip_ineligible_salary=bool(application.get("skip_ineligible_salary", True)),
            min_inr_salary_lpa=float(application.get("min_inr_salary_lpa", 25)),
            apply_retries=int(application.get("apply_retries", 1)),
            retry_backoff_ms=int(application.get("retry_backoff_ms", 1500)),
            interactive_questions=bool(application.get("interactive_questions", True)),
            confirm_new_answers=bool(application.get("confirm_new_answers", True)),
            rag_answer_questions=bool(application.get("rag_answer_questions", True)),
            one_job_per_company=bool(application.get("one_job_per_company", True)),
            enrich_workers=int(application.get("enrich_workers", 4)),
            pipeline_apply=bool(application.get("pipeline_apply", True)),
            platform_delays=_platform_delays(application.get("platform_delays", {})),
        ),
        browser=BrowserConfig(
            headless=bool(browser.get("headless", False)),
            slow_mo_ms=int(browser.get("slow_mo_ms", 100)),
            use_chrome_profile=bool(browser.get("use_chrome_profile", True)),
            chrome_user_data_dir=str(browser.get("chrome_user_data_dir", "")),
            chrome_profile_name=str(browser.get("chrome_profile_name", "Default")),
            chrome_channel=str(browser.get("chrome_channel", "chrome")),
        ),
        auth=AuthConfig(
            method=str(auth.get("method", "browser")),
            sessions_dir=str(auth.get("sessions_dir", "data/sessions")),
            login_timeout_seconds=int(auth.get("login_timeout_seconds", 300)),
        ),
        state=StateConfig(
            applied_jobs_file=str(state.get("applied_jobs_file", "data/applied_jobs.json")),
            log_file=str(state.get("log_file", "data/run.log")),
        ),
        paths=paths,
        answers=answers_policy,
    )
    # Set base_dir explicitly
    config.base_dir = base_dir

    # Merge the user's persisted drop-keyword blocklist (e.g. added by replying
    # "drop <keyword>" over Telegram) into the role-skip keywords so every future
    # run filters those titles out, exactly like config-defined skip_role_keywords.
    from .drop_keywords import load_drop_keywords

    extra_skip = load_drop_keywords(config)
    if extra_skip:
        existing = {k.strip().lower() for k in config.profile.skip_role_keywords}
        config.profile.skip_role_keywords.extend(k for k in extra_skip if k.lower() not in existing)
    return config
