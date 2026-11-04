"""Chroma vector DB (data/chroma, one collection per master data object) and OpenAI embeddings."""
from functools import lru_cache

import chromadb
from chromadb.config import Settings
from openai import OpenAI

from .config import CHROMA_DIR, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, MIN_SEMANTIC_SIMILARITY, OPENAI_BASE_URL


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
def db():
    return chromadb.PersistentClient(path=str(CHROMA_DIR), settings=Settings(anonymized_telemetry=False))


@lru_cache(maxsize=8)
def collection(name: str):
    # embeddings are always passed explicitly -> no default embedding function (would download a model)
    return db().get_or_create_collection(
        name, embedding_function=None,
        metadata={"hnsw:space": "cosine", "model": EMBEDDING_MODEL, "dimensions": EMBEDDING_DIMENSIONS},
    )


def drop(name: str) -> None:
    """Delete a collection (indexer --rebuild)."""
    try:
        db().delete_collection(name)
    except Exception:   # noqa: BLE001 -- not there yet
        pass
    collection.cache_clear()


def indexed_count(name: str) -> int:
    try:
        return collection(name).count()
    except Exception:   # noqa: BLE001 -- a missing or broken DB only disables the semantic search
        return 0


def query(name: str, text: str, limit: int) -> list[tuple[str, float]]:
    """[(id, cosine similarity)] of the documents closest to the query text."""
    col = collection(name)
    count = col.count()
    if not count:
        return []
    result = col.query(query_embeddings=[list(embed_query(text))], n_results=min(limit, count),
                       include=["distances"])
    return [(i, 1 - d) for i, d in zip(result["ids"][0], result["distances"][0]) if 1 - d >= MIN_SEMANTIC_SIMILARITY]


def neighbours(name: str, doc_id: str, limit: int) -> list[tuple[str, float]]:
    """[(id, cosine similarity)] of the documents closest to an indexed document (without itself)."""
    col = collection(name)
    if not col.count():
        return []
    own = col.get(ids=[doc_id], include=["embeddings"])
    if not len(own["ids"]):
        return []
    result = col.query(query_embeddings=[own["embeddings"][0]], n_results=min(limit + 1, col.count()),
                       include=["distances"])
    return [(i, 1 - d) for i, d in zip(result["ids"][0], result["distances"][0]) if i != doc_id]
