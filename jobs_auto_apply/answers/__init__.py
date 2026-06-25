"""
Answer pipeline building blocks.

Resolution order (``resolve.resolve_question_answers``):

  1. Saved memory (``memory_store``)
  2. Config / facts (``config_answers.authoritative_config_answer``)
  3. Rule RAG → vector top-1 → Ollama (``draft`` + ``llm_policy``)
  4. Defer / queue when unanswered
"""

from .config_answers import authoritative_config_answer
from .draft import DraftResult, clear_draft_answer_cache, draft_answer_for_field
from .experience import is_new_experience_question, is_skill_years_question
from .fields import enrich_field_for_llm, infer_field_input_type
from .llm_policy import effective_min_confidence, llm_decision_acceptable
from .memory_store import get_saved_answer, persist_answer, save_answer
from .resolve import resolve_question_answers
from .validation import answer_acceptable_for_field, needs_review_answer

__all__ = [
    "answer_acceptable_for_field",
    "authoritative_config_answer",
    "clear_draft_answer_cache",
    "draft_answer_for_field",
    "effective_min_confidence",
    "enrich_field_for_llm",
    "get_saved_answer",
    "infer_field_input_type",
    "is_new_experience_question",
    "is_skill_years_question",
    "llm_decision_acceptable",
    "needs_review_answer",
    "persist_answer",
    "resolve_question_answers",
    "save_answer",
]
