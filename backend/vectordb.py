"""Chroma vector DB (data/chroma, one collection per master data object) and OpenAI embeddings."""
from functools import lru_cache

import chromadb
import numpy as np
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


def similarities(name: str, text: str, ids: list[str]) -> dict[str, float]:
    """Exact cosine similarity of the query text to the given indexed documents (ids not in the index are missing).

    The nearest neighbour search (HNSW) is approximate and may miss close documents; this scores the candidates of
    the other retrievers exactly, from the stored embeddings (no extra embedding call)."""
    if not ids:
        return {}
    got = collection(name).get(ids=list(dict.fromkeys(ids)), include=["embeddings"])
    if not len(got["ids"]):
        return {}
    vectors = np.asarray(got["embeddings"], dtype=np.float32)
    q = np.asarray(embed_query(text), dtype=np.float32)
    sims = vectors @ q / (np.linalg.norm(vectors, axis=1) * np.linalg.norm(q) + 1e-12)
    return dict(zip(got["ids"], sims.tolist()))


def recall(name: str, queries: list[str], k: int = 100, page: int = 10000) -> float:
    """Share of the true k nearest neighbours that the HNSW search returns for the query texts (true neighbours by
    brute force, read page-wise so that large collections fit in memory)."""
    col = collection(name)
    count = col.count()
    if count <= k or not queries:
        return 1.0
    q = np.asarray(embed(queries), dtype=np.float32)
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    best_ids = [np.array([], dtype=object)] * len(q)
    best_sims = [np.array([], dtype=np.float32)] * len(q)
    for start in range(0, count, page):
        got = col.get(offset=start, limit=page, include=["embeddings"])
        vectors = np.asarray(got["embeddings"], dtype=np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        ids = np.asarray(got["ids"], dtype=object)
        sims = q @ vectors.T
        for i in range(len(q)):
            all_sims = np.concatenate([best_sims[i], sims[i]])
            all_ids = np.concatenate([best_ids[i], ids])
            top = np.argsort(-all_sims)[:k]
            best_sims[i], best_ids[i] = all_sims[top], all_ids[top]
    found = col.query(query_embeddings=q.tolist(), n_results=k, include=[])["ids"]
    return float(np.mean([len(set(f) & set(t.tolist())) / k for f, t in zip(found, best_ids)]))


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
