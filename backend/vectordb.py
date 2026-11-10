"""Chroma vector DB (data/chroma, one collection per master data object) and OpenAI embeddings."""
import re
import shutil
import sqlite3
from functools import lru_cache, wraps

import chromadb
import numpy as np
from chromadb.config import Settings
from chromadb.errors import NotFoundError
from openai import OpenAI

from .config import (CHROMA_DIR, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, HNSW_CONSTRUCTION_EF, HNSW_M, HNSW_SEARCH_EF,
                     MIN_SEMANTIC_SIMILARITY, OPENAI_BASE_URL)


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


def reopen_on_rebuild(fn):
    """Retry once with fresh collection handles when the collection was replaced meanwhile (indexer --rebuild or a
    graph rebuild in another process while the server is running)."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NotFoundError:
            collection.cache_clear()
            return fn(*args, **kwargs)
    return wrapper


HNSW = {"space": "cosine", "max_neighbors": HNSW_M, "ef_construction": HNSW_CONSTRUCTION_EF, "ef_search": HNSW_SEARCH_EF}
COPY_PAGE = 5000


def _create(name: str):
    # embeddings are always passed explicitly -> no default embedding function (would download a model)
    return db().get_or_create_collection(name, embedding_function=None, configuration={"hnsw": HNSW},
                                         metadata={"model": EMBEDDING_MODEL, "dimensions": EMBEDDING_DIMENSIONS})


@lru_cache(maxsize=8)
def collection(name: str):
    _recover(name)
    return _create(name)


def _recover(name: str) -> None:
    """Finish a graph rebuild that was interrupted between deleting the old and renaming the new collection."""
    names = {c.name for c in db().list_collections()}
    if name not in names and f"{name}__new" in names:
        db().get_collection(f"{name}__new").modify(name=name)


def graph_settings(name: str) -> dict:
    """HNSW settings the collection was built with."""
    return dict((collection(name).configuration or {}).get("hnsw") or {})


def graph_outdated(name: str) -> bool:
    built = graph_settings(name)
    return any(built.get(k) != HNSW[k] for k in ("space", "max_neighbors", "ef_construction", "ef_search"))


def rebuild_graph(name: str, progress=print) -> None:
    """Build the HNSW graph again with the configured settings from the stored embeddings (no embedding calls).

    M and ef_construction only apply when the graph is built, and entries added one by one over many runs connect
    worse than a fresh build. The documents are copied into a new collection, which then replaces the old one."""
    old = collection(name)
    tmp = f"{name}__new"
    drop(tmp)
    new = _create(tmp)
    count = old.count()
    for start in range(0, count, COPY_PAGE):
        got = old.get(offset=start, limit=COPY_PAGE, include=["embeddings", "documents", "metadatas"])
        new.add(ids=got["ids"], embeddings=got["embeddings"], documents=got["documents"], metadatas=got["metadatas"])
        progress(f"  {min(start + COPY_PAGE, count)}/{count}")
    if new.count() != count:
        drop(tmp)
        raise RuntimeError(f"Kopie von '{name}' unvollständig ({new.count()} statt {count})")
    db().delete_collection(name)
    new.modify(name=name)
    collection.cache_clear()
    remove_orphans()


def remove_orphans() -> None:
    """Delete the HNSW folders of deleted collections, which Chroma leaves on disk (only folders named by a segment
    id that no segment in chroma.sqlite3 refers to any more)."""
    try:
        with sqlite3.connect(f"file:{CHROMA_DIR / 'chroma.sqlite3'}?mode=ro", uri=True) as con:
            used = {row[0] for row in con.execute("SELECT id FROM segments")}
    except sqlite3.Error:
        return
    for folder in CHROMA_DIR.iterdir():
        if folder.is_dir() and re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", folder.name) \
                and folder.name not in used:
            shutil.rmtree(folder, ignore_errors=True)


def drop(name: str) -> None:
    """Delete a collection (indexer --rebuild)."""
    try:
        db().delete_collection(name)
    except Exception:   # noqa: BLE001 -- not there yet
        pass
    collection.cache_clear()
    remove_orphans()


@reopen_on_rebuild
def _count(name: str) -> int:
    return collection(name).count()


def indexed_count(name: str) -> int:
    try:
        return _count(name)
    except Exception:   # noqa: BLE001 -- a missing or broken DB only disables the semantic search
        return 0


@reopen_on_rebuild
def query(name: str, text: str, limit: int) -> list[tuple[str, float]]:
    """[(id, cosine similarity)] of the documents closest to the query text."""
    col = collection(name)
    count = col.count()
    if not count:
        return []
    result = col.query(query_embeddings=[list(embed_query(text))], n_results=min(limit, count),
                       include=["distances"])
    return [(i, 1 - d) for i, d in zip(result["ids"][0], result["distances"][0]) if 1 - d >= MIN_SEMANTIC_SIMILARITY]


@reopen_on_rebuild
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


@reopen_on_rebuild
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
