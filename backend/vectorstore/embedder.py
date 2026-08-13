"""Embedding provider. Default is a local open-source model (no API key needed).

To switch to a hosted embedding service (Voyage AI, OpenAI, ...), add a branch below
and set EMBEDDING_PROVIDER accordingly — nothing outside this file needs to change.
Note: db/models.py:EMBEDDING_DIM must match the chosen model's output dimension.
"""

from functools import lru_cache

from core.config import get_settings


@lru_cache
def _local_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(get_settings().embedding_model)


def embed_texts(texts: list[str]) -> list[list[float]]:
    settings = get_settings()

    if settings.embedding_provider == "local":
        model = _local_model()
        return model.encode(texts, normalize_embeddings=True).tolist()

    raise NotImplementedError(
        f"Embedding provider {settings.embedding_provider!r} is not wired up yet. "
        "Add a branch in vectorstore/embedder.py:embed_texts."
    )


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]
