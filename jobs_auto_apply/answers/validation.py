from __future__ import annotations

import logging
import re
from typing import Any

from ..answer_suggest import is_employer_check_question, is_prior_application_screening
from ..config import AppConfig
from .profile_links import is_github_profile_question
from .compensation import (
    compensation_answer_compatible,
    looks_like_compensation_question,
    resolve_ctc_numeric_answer,
)
from .config_answers import ctc_want_kind
from .experience import is_skill_years_question
from .fields import (
    OPTIONAL_NAME_FIELD,
    enrich_field_for_llm,
    infer_field_input_type,
    is_last_working_day_question,
    is_numeric_ctc_question,
    is_pincode_field,
)
from .chips import is_chip_range_label, parse_years_numeric_value, _value_in_answer_range as value_in_answer_range_legacy
from .location import is_location_value_question, location_like_answer_fits

logger = logging.getLogger("job_apply")

_LLM_META_ANSWER = re.compile(
    r"rag suggestion|verified profile|conservatively|years of experience \(profile\)|"
    r"no verified .+ experience|profile includes|serving_notice\s*:|"
    r"willing_to_relocate\s*:|last_working_day\s*:|preferred_locations\s*:|"
    r"interview_available\s*:|langchain_years|skill_years\s*:",
    re.I,
)

def is_placeholder_answer(answer: str) -> bool:
    """Detect answers that are tips/placeholders rather than real responses."""
    a = answer.strip().lower()
    if not a:
        return True
    return "e.g." in a or a.startswith("tip:") or "— e.g." in a


_LLM_META_ANSWER = re.compile(
    r"rag suggestion|verified profile|conservatively|years of experience \(profile\)|"
    r"no verified .+ experience|profile includes|serving_notice\s*:|"
    r"willing_to_relocate\s*:|last_working_day\s*:|preferred_locations\s*:|"
    r"interview_available\s*:|langchain_years|skill_years\s*:",
    re.I,
)



def is_llm_meta_answer(answer: str) -> bool:
    """LLM echoed prompt context or RAG commentary instead of a field value."""
    return bool(_LLM_META_ANSWER.search(answer.strip()))



def needs_review_answer(question: str, answer: str) -> bool:
    """Saved answer looks wrong (e.g. RAG dumped resume text into a short field)."""
    if is_placeholder_answer(answer):
        return True
    q = question.lower()
    a = answer.strip()
    if re.search(r"\bnotice\s*period\b|\bnp\b|how\s+soon.*join|available\s+to\s+join", q):
        if re.fullmatch(r"\d+(?:\s*days?)?", a, re.I):
            return False
        if re.search(r"\b(immediate|immediately|serving|available)\b", a, re.I):
            return False
    if is_llm_meta_answer(a):
        return True
    input_type = infer_field_input_type(q)
    if re.search(r"^no verified\b", a, re.I) and (
        is_skill_years_question(q) or input_type == "years_numeric"
    ):
        return True
    if len(a) > 150 and any(
        token in q
        for token in (
            "how many",
            "years",
            "ctc",
            "salary",
            "usd",
            "monthly",
            "url",
            "linkedin",
            "when would",
            "available to start",
            "rate your",
            "comfortable",
            "based in india",
            "cet working",
            "yes",
            "no",
        )
    ):
        return True
    if "linkedin" in q and "linkedin.com" not in a.lower():
        return True
    if "github" in q and "github.com" not in a.lower():
        if is_github_profile_question(q):
            return True
    if a.lower().startswith("results-driven"):
        return True
    if re.search(r"\boffers?\b", q) and len(a) > 20:
        return True
    if is_employer_check_question(q) and (
        len(a) > 40 or not re.match(r"^(yes|no)\b", a, re.I)
    ):
        return True
    if is_prior_application_screening(q) and (
        len(a) > 8 or not re.match(r"^(yes|no)\b", a, re.I)
    ):
        return True
    if looks_like_compensation_question(q):
        if re.fullmatch(r"\d+(?:\.\d+)?(?:\s*lpa)?", a, re.I):
            return False
        if re.fullmatch(r"\d+(?:\.\d+)?/\d+(?:\.\d+)?", a):
            return False
        if (
            re.search(r"\bcurrent\b", a, re.I)
            and re.search(r"\bexpected\b", a, re.I)
        ):
            return False
    if (
        input_type == "short_text"
        and re.fullmatch(r"\d+(?:\.\d+)?", a)
        and not is_skill_years_question(q)
        and input_type != "years_numeric"
        and not is_numeric_ctc_question(q)
        and not is_pincode_field(q)
    ):
        return True
    if input_type == "years_numeric" and parse_years_numeric_value(a) is None:
        if re.fullmatch(r"yes|no", a, re.I):
            return False
        return True
    if is_numeric_ctc_question(q):
        parsed = resolve_ctc_numeric_answer(q, a, None)
        if parsed and re.fullmatch(r"\d+(?:\.\d+)?/\d+(?:\.\d+)?", parsed):
            return False
        if not parsed or not re.fullmatch(r"\d+(?:\.\d+)?", parsed):
            if (
                re.search(r"\bcurrent\b", a, re.I)
                and re.search(r"\bexpected\b", a, re.I)
            ):
                return False
            if re.search(r"\b(current|expected)\b", a, re.I) and len(a) > 12:
                return True
        want = ctc_want_kind(q)
        if want == "expected" and re.search(r"\bcurrent\b", a, re.I) and not re.search(
            r"\bexpected\b", a, re.I
        ):
            return True
        if want == "current" and re.search(r"\bexpected\b", a, re.I) and not re.search(
            r"\bcurrent\b", a, re.I
        ):
            return True
    if not compensation_answer_compatible(q, a):
        return True
    if is_skill_years_question(q):
        years = parse_years_numeric_value(a)
        if years is None:
            return True
        if years == 0:
            return False
        if not re.fullmatch(r"\d+(?:\.\d+)?(?:\s*years?)?", a, re.I):
            return True
    if is_location_value_question(q) and not location_like_answer_fits(q, a):
        return True
    if is_last_working_day_question(q):
        if re.search(r"\b\d+\s*days?\b", a, re.I) and not re.search(
            r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", a
        ):
            return True
        if re.search(r"serving_notice\s*:|last_working_day\s*:", a, re.I):
            return True
        if not re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", a.strip()):
            if re.search(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", a) and len(a) > 12:
                return True
    return False


def normalize_employer_radio_answer(question: str, answer: str) -> str:
    """Map employer-check prose to Yes/No for radio fields."""
    if not is_employer_check_question(question) and not is_prior_application_screening(question):
        return answer.strip()
    text = answer.strip()
    if not text:
        return text
    if re.fullmatch(r"yes", text, re.I):
        return "Yes"
    if re.fullmatch(r"no", text, re.I):
        return "No"
    lower = text.lower()
    if re.search(r"\bno\b", lower) and not re.search(r"\byes\b", lower):
        return "No"
    if re.search(r"\byes\b", lower):
        return "Yes"
    return text



def answer_acceptable_for_field(
    question: str,
    answer: str,
    field: dict[str, Any],
) -> bool:
    """Reject resume dumps, placeholders, and answers that don't fit field options."""
    if not answer or is_placeholder_answer(answer):
        return False
    # An answer that is literally one of the field's own options is always a valid
    # fill for that field (a constrained pick can't be malformed), so it bypasses
    # the type heuristics below (e.g. years/CTC numeric checks reject range labels
    # like "5 - 7years" even though they are real options).
    if _answer_is_exact_option(answer, field):
        return True
    if needs_review_answer(question, answer):
        return False
    if not saved_answer_fits_field(answer, field):
        return False
    return True


def _answer_is_exact_option(answer: str, field: dict[str, Any]) -> bool:
    options = field.get("options") or field.get("answer_options") or []
    if not options:
        return False
    want = re.sub(r"\s+", " ", str(answer).strip().lower())
    if not want:
        return False
    for opt in options:
        if re.sub(r"\s+", " ", str(opt).strip().lower()) == want:
            return True
    return False





def saved_answer_fits_field(
    answer: str,
    field: dict[str, Any],
    config: AppConfig | None = None,
) -> bool:
    """Reject cross-group reuse when a text answer cannot fill a Yes/No control."""
    label = str(field.get("label", ""))
    if not compensation_answer_compatible(label, answer):
        return False
    if is_pincode_field(label):
        a = answer.strip().lower()
        if a in ("yes", "no"):
            return False
        return bool(re.fullmatch(r"\d{4,8}", answer.strip()))
    from ..question_groups import classify_question

    if classify_question(label) == "current_location" and not location_like_answer_fits(
        label, answer, field
    ):
        return False
    if classify_question(label) == "preferred_location" and not location_like_answer_fits(
        label, answer, field
    ):
        return False
    if is_employer_check_question(label):
        normalized = normalize_employer_radio_answer(label, answer)
        if normalized not in ("Yes", "No"):
            return False
    if is_prior_application_screening(label):
        normalized = normalize_employer_radio_answer(label, answer)
        if normalized not in ("Yes", "No"):
            return False
    if is_numeric_ctc_question(label) or infer_field_input_type(label, field) == "ctc_numeric":
        want = ctc_want_kind(label)
        parsed = resolve_ctc_numeric_answer(label, answer, config)
        if want == "both":
            if parsed and re.fullmatch(r"\d+(?:\.\d+)?/\d+(?:\.\d+)?", parsed):
                return True
            return False
        if not parsed or not re.fullmatch(r"\d+(?:\.\d+)?", parsed):
            return False
        if want == "expected" and re.search(r"\bcurrent\b", answer, re.I) and not re.search(
            r"\bexpected\b", answer, re.I
        ):
            return False
        if want == "current" and re.search(r"\bexpected\b", answer, re.I) and not re.search(
            r"\bcurrent\b", answer, re.I
        ):
            return False
        return True
    input_type = infer_field_input_type(label, field)
    if input_type == "years_numeric" or is_skill_years_question(label):
        years = parse_years_numeric_value(answer)
        if years is None and not is_chip_range_label(answer):
            return False
        if years is not None and not re.fullmatch(r"\d+(?:\.\d+)?(?:\s*years?)?", answer.strip(), re.I):
            if len(answer.strip()) > 12:
                return False
    if is_last_working_day_question(label):
        if re.search(r"\b\d+\s*days?\b", answer, re.I) and not re.search(
            r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", answer
        ):
            return False
        if re.search(r"serving_notice\s*:|last_working_day\s*:", answer, re.I):
            return False
        return bool(re.search(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", answer))
    if re.search(r"\bnotice\s*period\b|\bnp\b", label, re.I):
        if re.fullmatch(r"\d+(?:\s*days?)?", answer.strip(), re.I):
            return True
        if re.search(r"\b(immediate|immediately|serving|available)\b", answer, re.I):
            return True
    if OPTIONAL_NAME_FIELD.search(label):
        if re.fullmatch(r"\d+", answer.strip()):
            return False
        if re.fullmatch(r"(yes|no)", answer.strip(), re.I):
            return False
    kind = str(field.get("kind", "text"))
    if kind in ("radio", "checkbox") and len(answer.strip()) > 80:
        return False
    options = [str(o).strip() for o in field.get("options", []) if str(o).strip()]
    a = answer.strip().lower()
    if kind in ("text", "short_text", "textarea") and options:
        opts = [o.lower() for o in options]
        if set(opts).issubset({"yes", "no"}) and len(opts) >= 2:
            if re.fullmatch(r"yes|no", a, re.I):
                return True
            if len(a) <= 12 and a in ("yes", "no", "y", "n"):
                return True
    if kind not in ("radio", "checkbox"):
        input_type = infer_field_input_type(label, field)
        if (
            input_type == "short_text"
            and re.fullmatch(r"\d+(?:\.\d+)?", answer.strip())
            and not is_skill_years_question(label)
            and not is_numeric_ctc_question(label)
            and not is_pincode_field(label)
        ):
            return False
        return True
    if kind == "radio" and options:
        opts = [o.lower() for o in options]
        if set(opts).issubset({"yes", "no"}) and len(opts) >= 2:
            if re.fullmatch(r"yes|no", a):
                return a in ("yes", "no")
            if is_employer_check_question(label):
                return normalize_employer_radio_answer(label, answer) in ("Yes", "No")
            return len(a) <= 8 and a in ("yes", "no")
        if any(a == o for o in opts):
            return True
        if len(a) <= 24:
            if any(a in o or o in a for o in opts):
                return True
        num = re.search(r"(\d+)", a)
        if num:
            value = int(num.group(1))
            if any(value_in_answer_range_legacy(value, o) for o in options):
                return True
        if any(w in a for w in ("immediate", "lwd", "serving")) and any(
            re.search(r"immediate|serving", o, re.I) for o in options
        ):
            return True
        return False
    if kind == "checkbox":
        return a in ("yes", "no", "true", "false", "1", "0", "checked", "agree", "accept")
    if kind == "checkbox_group" and options:
        label = str(field.get("label", ""))
        if re.search(
            r"\bselect\b.{0,30}\b(city|cities)\b|\b(city|cities)\b.{0,30}\b(residing|relocate)\b",
            label,
            re.I,
        ):
            if a in ("yes", "y"):
                return True
            for part in re.split(r"[,;|]", answer):
                token = part.strip().lower().split(",")[0].strip()
                if len(token) < 3:
                    continue
                if any(token in o.lower() or o.lower().startswith(token) for o in options):
                    return True
            return False
        for opt in options:
            ol = opt.lower()
            if a == ol or a in ol or ol in a:
                return True
        return False
    return True



_YESNO_ONLY = frozenset(
    {"yes", "no", "y", "n", "true", "false", "yeah", "yep", "nope", "na", "n/a"}
)


def requires_personal_artifact(question: str) -> bool:
    """Question that demands a personal artifact the bot cannot synthesize.

    These ask the candidate to paste/share a link (GitHub/repo/demo/portfolio) or
    give a concrete example of something they built, optionally with a short
    writeup ("...and in 2-3 lines tell what it does"). No profile fact, RAG rule,
    or LLM guess can truthfully fill these — they must be answered by the user, so
    callers should always queue them rather than auto-fill.
    """
    q = str(question or "").lower()
    # "paste/share/provide/attach a link/url/github/repo/demo/portfolio"
    if re.search(
        r"\b(paste|share|provide|attach|drop|enter|add|send|give)\b[^?]{0,40}"
        r"\b(link|url|links?|github|gitlab|bitbucket|repo(?:sitory)?|demo|"
        r"portfolio|profile|sample)\b",
        q,
    ):
        return True
    # "link/url to/of a project/work/app/agent/build you built/shipped"
    if re.search(
        r"\b(link|url)\b[^?]{0,30}\b(to|of|for)\b[^?]{0,40}"
        r"\b(project|work|app|product|agent|llm|build|built|demo|repo)\b",
        q,
    ):
        return True
    # "give/share/show an example of ..."
    if re.search(r"\b(give|share|show)\b[^?]{0,30}\bexamples?\b", q):
        return True
    return False


def is_open_ended_describe_question(question: str) -> bool:
    """Question that asks the candidate to describe/list specifics.

    A bare Yes/No can never satisfy it (e.g. "What experience do you have in the
    Automotive domain, and which automotive projects have you worked on?").
    Kept deliberately narrow so genuine Yes/No questions are not caught.
    """
    q = str(question or "").lower()
    if re.search(
        r"\b(describe|elaborate|walk (us|me) through|please (specify|elaborate)|"
        r"tell us about)\b",
        q,
    ):
        return True
    if re.search(r"\bwhat\b.*\bexperience\b.*\band which\b", q):
        return True
    if requires_personal_artifact(q):
        return True
    return False


def is_hard_type_mismatch(
    question: str,
    answer: str,
    field: dict[str, Any] | None,
    config: AppConfig | None = None,
) -> bool:
    """A bare Yes/No answer can never fill a numeric or open-ended field.

    This is the one mismatch we enforce even for human-reviewed saved answers,
    because it is always a cross-question reuse bug (e.g. a yes/no "do you have
    AI/ML experience?" answer bleeding into a "how much AI/ML experience?" numeric
    field, or a stray "No" saved against a "describe your experience" prompt)
    rather than a legitimately-formatted answer the user typed.
    """
    a = answer.strip().lower()
    if a not in _YESNO_ONLY:
        return False
    label = str((field or {}).get("label", "") or question)
    if is_open_ended_describe_question(label):
        return True
    numeric = (
        infer_field_input_type(label, field or {}) in ("years_numeric", "ctc_numeric")
        or is_skill_years_question(label)
        or is_numeric_ctc_question(label)
    )
    return numeric


def answer_usable(
    question: str,
    answer: str,
    field: dict[str, Any] | None,
    config: AppConfig | None = None,
) -> bool:
    if needs_review_answer(question, answer):
        return False
    if field and not saved_answer_fits_field(answer, field, config):
        return False
    return True



def _yes_no_equivalent(a: str, b: str) -> bool:
    yes = {"yes", "y", "true", "1"}
    no = {"no", "n", "false", "0"}
    al, bl = a.strip().lower(), b.strip().lower()
    if al == bl:
        return True
    if al in yes and bl in yes:
        return True
    return al in no and bl in no



def answers_equivalent_for_agreement(
    question: str,
    field: dict[str, Any],
    answer_a: str,
    answer_b: str,
    config: AppConfig | None = None,
) -> bool:
    from .memory_store import canonicalize_stored_answer, resolve_fill_answer

    field = enrich_field_for_llm(field)
    fill_a = resolve_fill_answer(
        canonicalize_stored_answer(question, answer_a, field, config),
        field,
        config,
    )
    fill_b = resolve_fill_answer(
        canonicalize_stored_answer(question, answer_b, field, config),
        field,
        config,
    )
    al, bl = fill_a.strip().lower(), fill_b.strip().lower()
    if al == bl:
        return True
    if _yes_no_equivalent(al, bl):
        return True
    num_a = parse_years_numeric_value(al)
    num_b = parse_years_numeric_value(bl)
    if num_a is not None and num_b is not None:
        return num_a == num_b
    return False



