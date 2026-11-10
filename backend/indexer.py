"""Embed the master data from the input folder into the Chroma DB (one collection per object).

    python -m backend.indexer                     # materials and business partners: new and changed ones (resumable)
    python -m backend.indexer partner             # only business partners (or: material)
    python -m backend.indexer partner --rebuild   # drop the collection and embed everything again
    python -m backend.indexer --graph             # build the HNSW graph again from the stored embeddings
    python -m backend.indexer --check             # only measure the recall of the vector search (no embedding)

Objects whose document did not change (content hash in the metadata) are skipped, so an interrupted run
can simply be started again.
"""
import argparse
import random
import sys
import time

import openai

from . import vectordb
from .config import (EMBEDDING_BATCH, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, MATERIAL_COLLECTION, OPENAI_BASE_URL,
                     PARTNER_COLLECTION)


def load(kind: str) -> dict:
    if kind == "material":
        from .material.data import load_materials
        return load_materials()
    from .partner.data import load_partners
    return load_partners()


RECALL_SAMPLES, RECALL_K, RECALL_WARN = 50, 100, 0.95
OBJECTS = {"material": (MATERIAL_COLLECTION, "Materialien"), "partner": (PARTNER_COLLECTION, "Geschäftspartner")}


def index(kind: str, rebuild: bool) -> None:
    name, label = OBJECTS[kind]
    if rebuild:
        vectordb.drop(name)
    objects = load(kind)
    col = vectordb.collection(name)
    existing = col.get(include=["metadatas"])
    hashes = {i: (m or {}).get("hash") for i, m in zip(existing["ids"], existing["metadatas"])}

    stale = [i for i in hashes if i not in objects]
    if stale:
        col.delete(ids=stale)
    todo = [o for o in objects.values() if hashes.get(o.id) != o.content_hash()]
    print(f"{len(objects)} {label}, {len(objects) - len(todo)} aktuell im Index '{name}', {len(stale)} entfernt, "
          f"{len(todo)} zu embedden mit {EMBEDDING_MODEL} ({EMBEDDING_DIMENSIONS} Dim.) "
          f"über {OPENAI_BASE_URL or 'api.openai.com'}")

    started = time.time()
    for start in range(0, len(todo), EMBEDDING_BATCH):
        batch = todo[start:start + EMBEDDING_BATCH]
        docs = [o.document() for o in batch]
        for attempt in range(6):
            try:
                embeddings = vectordb.embed(docs)
                break
            except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as e:
                wait = 2 ** attempt * 5
                print(f"  {type(e).__name__} - neuer Versuch in {wait} s")
                time.sleep(wait)
        else:
            sys.exit("Abbruch: zu viele Fehler beim Embedding - Lauf später erneut starten (setzt fort)")
        col.upsert(ids=[o.id for o in batch], embeddings=embeddings, documents=docs,
                   metadatas=[{"hash": o.content_hash(), **o.index_metadata()} for o in batch])
        print(f"  {start + len(batch)}/{len(todo)} ({time.time() - started:.0f} s)")
    print(f"Fertig: {col.count()} {label} im Vektorindex '{name}'")


def graph(kind: str, force: bool) -> None:
    """Build the HNSW graph again if its settings differ from the configured ones (or on request)."""
    name, label = OBJECTS[kind]
    if not vectordb.indexed_count(name) or not (force or vectordb.graph_outdated(name)):
        return
    built = vectordb.graph_settings(name)
    print(f"{label}: HNSW-Graph neu aufbauen aus den gespeicherten Embeddings "
          f"(M {built.get('max_neighbors')} -> {vectordb.HNSW['max_neighbors']}, "
          f"ef_construction {built.get('ef_construction')} -> {vectordb.HNSW['ef_construction']}, "
          f"ef_search {built.get('ef_search')} -> {vectordb.HNSW['ef_search']})")
    started = time.time()
    vectordb.rebuild_graph(name)
    print(f"  fertig ({time.time() - started:.0f} s)")


def check(kind: str) -> None:
    """Recall of the approximate (HNSW) nearest neighbour search against brute force."""
    name, label = OBJECTS[kind]
    if not vectordb.indexed_count(name):
        print(f"{label}: Vektorindex '{name}' ist leer")
        return
    # short queries like a user types them: the first words of the documents of random objects
    objects = list(load(kind).values())
    sample = random.Random(42).sample(objects, min(RECALL_SAMPLES, len(objects)))
    queries = list(dict.fromkeys(" ".join(o.document().split()[:3]) for o in sample))
    started = time.time()
    value = vectordb.recall(name, queries, RECALL_K)
    note = "" if value >= RECALL_WARN else (f" - unter {RECALL_WARN:.0%}, die Vektorsuche verfehlt nahe Treffer "
                                             "(Graph neu aufbauen: --graph, ggf. MDSA_HNSW_* erhöhen)")
    print(f"{label}: Recall@{RECALL_K} der Vektorsuche {value:.1%} "
          f"({RECALL_SAMPLES} Stichproben, {time.time() - started:.0f} s){note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("objects", nargs="*", metavar="material|partner", help="objects to index (default: all)")
    parser.add_argument("--rebuild", action="store_true", help="delete the collection(s) and embed everything again")
    parser.add_argument("--graph", action="store_true",
                        help="build the HNSW graph again from the stored embeddings (no embedding calls)")
    parser.add_argument("--check", action="store_true", help="only measure the recall of the vector search")
    args = parser.parse_args()
    if unknown := set(args.objects) - set(OBJECTS):
        parser.error(f"unbekanntes Objekt: {', '.join(sorted(unknown))} (material oder partner)")
    for kind in args.objects or list(OBJECTS):
        try:
            if not args.check:
                index(kind, args.rebuild)
                graph(kind, args.graph)
            check(kind)
        except FileNotFoundError as e:
            print(f"{OBJECTS[kind][1]} übersprungen: {e}")


if __name__ == "__main__":
    main()
