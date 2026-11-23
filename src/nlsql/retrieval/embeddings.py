"""Local fastembed text embeddings (``settings.embedding_model``) for the value index.

Qdrant's client embeds glossary text itself; Elasticsearch needs the vectors from us. Both use
the same model setting. Embedding is CPU work: call these from a thread (``asyncio.to_thread``).
"""

from functools import lru_cache

from fastembed import TextEmbedding

from nlsql.core.config import get_settings

BATCH = 256


@lru_cache
def _model() -> TextEmbedding:
    return TextEmbedding(model_name=get_settings().embedding_model)


def dims() -> int:
    """Vector size of the configured model (no model download needed)."""
    return TextEmbedding.get_embedding_size(get_settings().embedding_model)


def embed(texts: list[str]) -> list[list[float]]:
    """One vector per text, in order."""
    if not texts:
        return []
    return [v.tolist() for v in _model().embed(texts, batch_size=BATCH)]
