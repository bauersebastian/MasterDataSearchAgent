"""Embed all materials from the input folder into the bundled Chroma DB.

    python -m backend.indexer            # embed new and changed materials (resumable)
    python -m backend.indexer --rebuild  # drop the collection and embed everything again

Materials whose document did not change (content hash in the metadata) are skipped, so an interrupted run
can simply be started again.
"""
import argparse
import shutil
import sys
import time

import openai

from . import vectordb
from .config import CHROMA_DIR, EMBEDDING_BATCH, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, OPENAI_BASE_URL
from .data import load_materials


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rebuild", action="store_true", help="delete the vector DB and embed everything again")
    args = parser.parse_args()

    if args.rebuild and CHROMA_DIR.exists():
        shutil.rmtree(CHROMA_DIR)
    materials = load_materials()
    col = vectordb.collection()
    existing = col.get(include=["metadatas"])
    hashes = {i: (m or {}).get("hash") for i, m in zip(existing["ids"], existing["metadatas"])}

    stale = [i for i in hashes if i not in materials]
    if stale:
        col.delete(ids=stale)
    todo = [m for m in materials.values() if hashes.get(m.id) != m.content_hash()]
    print(f"{len(materials)} Materialien, {len(materials) - len(todo)} aktuell im Index, {len(stale)} entfernt, "
          f"{len(todo)} zu embedden mit {EMBEDDING_MODEL} ({EMBEDDING_DIMENSIONS} Dim.) "
          f"über {OPENAI_BASE_URL or 'api.openai.com'}")

    started = time.time()
    for start in range(0, len(todo), EMBEDDING_BATCH):
        batch = todo[start:start + EMBEDDING_BATCH]
        docs = [m.document() for m in batch]
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
        col.upsert(
            ids=[m.id for m in batch], embeddings=embeddings, documents=docs,
            metadatas=[{"hash": m.content_hash(), "mtart": m.mara.get("MTART", ""),
                        "matkl": m.mara.get("MATKL", "")} for m in batch],
        )
        done = start + len(batch)
        print(f"  {done}/{len(todo)} ({time.time() - started:.0f} s)")
    print(f"Fertig: {col.count()} Materialien im Vektorindex {CHROMA_DIR}")


if __name__ == "__main__":
    main()
