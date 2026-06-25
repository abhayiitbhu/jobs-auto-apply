"""CPU embedding model for FAISS — SentenceTransformer (avoids HuggingFaceEmbeddings meta-tensor bug)."""

from __future__ import annotations

import logging
import threading
from typing import Any

from langchain_core.embeddings import Embeddings

logger = logging.getLogger("job_apply")

_CACHE: dict[str, Any] = {}
_LOCK = threading.Lock()


class SentenceTransformerEmbeddings(Embeddings):
    """LangChain Embeddings via sentence-transformers on CPU."""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name, device="cpu")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            texts,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return vectors.tolist()

    def embed_query(self, text: str) -> list[float]:
        vector = self._model.encode(
            text,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return vector.tolist()


def get_embeddings(model_name: str) -> SentenceTransformerEmbeddings | None:
    """Return a cached embedding model, or None if load fails."""
    key = model_name.strip()
    if not key:
        return None

    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]

        try:
            emb = SentenceTransformerEmbeddings(key)
        except Exception as exc:
            logger.warning("Embedding model unavailable (%s): %s", key, exc)
            _CACHE[key] = None
            return None

        _CACHE[key] = emb
        logger.debug("Loaded embedding model: %s", key)
        return emb
