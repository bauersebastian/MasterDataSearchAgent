"""Chroma vector DB (bundled in data/chroma) and OpenAI embeddings."""
from functools import lru_cache

import chromadb
from chromadb.config import Settings
from openai import OpenAI

from .config import CHROMA_DIR, COLLECTION, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, OPENAI_BASE_URL


@lru_cache(maxsize=1)
def client() -> OpenAI:
    return OpenAI(base_url=OPENAI_BASE_URL)


def embed(texts: list[str]) -> list[list[float]]:
    response = client().embeddings.create(model=EMBEDDING_MODEL, input=texts, dimensions=EMBEDDING_DIMENSIONS)
    return [d.embedding for d in sorted(response.data, key=lambda d: d.index)]


@lru_cache(maxsize=512)
def embed_query(text: str) -> tuple[float, ...]:
    return tuple(embed([text])[0])


@lru_cache(maxsize=1)
def collection():
    db = chromadb.PersistentClient(path=str(CHROMA_DIR), settings=Settings(anonymized_telemetry=False))
    # embeddings are always passed explicitly -> no default embedding function (would download a model)
    return db.get_or_create_collection(
        COLLECTION, embedding_function=None,
        metadata={"hnsw:space": "cosine", "model": EMBEDDING_MODEL, "dimensions": EMBEDDING_DIMENSIONS},
    )


def indexed_count() -> int:
    try:
        return collection().count()
    except Exception:   # noqa: BLE001 -- a missing or broken DB only disables the semantic search
        return 0
