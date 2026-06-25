"""
Backward-compatible facade for the answer pipeline.

Prefer direct imports from ``jobs_auto_apply.answers`` submodules:

  answers.resolve      — resolve_question_answers (main orchestrator)
  answers.memory_store — saved Q&A in user_memory.json
  answers.draft        — RAG hint → LLM draft
  answers.validation   — answer quality checks
  answers.fields       — field type inference
  answers.config_answers — compensation, location, profile links from config
"""

from __future__ import annotations

from .question_keys import question_key

# Re-export public API used by platforms and scripts
from .answers.chips import (
    _normalize_to_option,
    is_chip_range_label,
    parse_years_numeric_value,
)
from .answers.compensation import (
    looks_like_compensation_question as _looks_like_compensation_question,
    resolve_ctc_numeric_answer,
)
from .answers.draft import (
    clear_draft_answer_cache,
    draft_answer_for_field,
    draft_answers_for_fields,
)
from .answers.experience import is_new_experience_question, is_skill_years_question
from .answers.fields import (
    enrich_field_for_llm,
    infer_field_for_question as _infer_field_for_question,
    infer_field_input_type,
    is_last_working_day_question,
    is_numeric_ctc_question,
    is_pincode_field,
)
from .answers.labels import (
    is_generic_question_label,
    is_plausible_application_question,
    normalize_question_label,
)
from .answers.llm_policy import effective_min_confidence, llm_decision_acceptable
from .answers.location import (
    is_location_value_question as _is_location_value_question,
    is_relocation_yesno_question as _is_relocation_yesno_question,
)
from .answers.memory_store import (
    canonicalize_stored_answer,
    flag_rejected_saved_answer,
    get_saved_answer,
    persist_answer,
    resolve_fill_answer,
    save_answer,
)
from .answers.resolve import resolve_question_answers
from .answers.validation import (
    answer_acceptable_for_field,
    is_llm_meta_answer,
    is_placeholder_answer,
    needs_review_answer,
)
from .answers.wellfound_dom import discover_questions, fill_questions

__all__ = [
    "answer_acceptable_for_field",
    "canonicalize_stored_answer",
    "clear_draft_answer_cache",
    "discover_questions",
    "draft_answer_for_field",
    "draft_answers_for_fields",
    "effective_min_confidence",
    "enrich_field_for_llm",
    "fill_questions",
    "flag_rejected_saved_answer",
    "get_saved_answer",
    "infer_field_input_type",
    "is_chip_range_label",
    "is_generic_question_label",
    "is_last_working_day_question",
    "is_llm_meta_answer",
    "is_new_experience_question",
    "is_numeric_ctc_question",
    "is_pincode_field",
    "is_placeholder_answer",
    "is_plausible_application_question",
    "is_skill_years_question",
    "llm_decision_acceptable",
    "needs_review_answer",
    "normalize_question_label",
    "parse_years_numeric_value",
    "persist_answer",
    "question_key",
    "resolve_ctc_numeric_answer",
    "resolve_fill_answer",
    "resolve_question_answers",
    "save_answer",
]
