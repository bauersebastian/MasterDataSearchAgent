"""Hybrid material search: exact keys (material number, EAN, vendor part number, old material number), fuzzy text
search and semantic search, fused with Reciprocal Rank Fusion (engine in backend/textsearch.py)."""
from collections import Counter
from functools import lru_cache
from typing import Optional

from .. import textsearch
from ..config import CANDIDATES, MATERIAL_COLLECTION, MAX_RESULTS
from ..textsearch import LexicalIndex, highlight
from .data import Material, load_materials


@lru_cache(maxsize=1)
def index() -> LexicalIndex:
    return LexicalIndex(load_materials())


def matches_filters(m: Material, filters: dict) -> bool:
    if not filters.get("include_deleted") and m.deleted:
        return False
    if filters.get("mtart") and m.mara.get("MTART") not in filters["mtart"]:
        return False
    if filters.get("matkl") and m.mara.get("MATKL") not in filters["matkl"]:
        return False
    if filters.get("vendor") and not set(filters["vendor"]) & set(m.vendors):
        return False
    return True


def search(query: str, mode: str = "hybrid", filters: Optional[dict] = None, limit: int = 50) -> dict:
    """mode: hybrid | semantic | lexical. Returns ranked hits with the score of each retriever."""
    filters = filters or {}
    materials = load_materials()
    query = query.strip()
    pool = CANDIDATES * (5 if any(filters.get(k) for k in ("mtart", "matkl", "vendor")) else 1)
    ranking = textsearch.rank(index(), MATERIAL_COLLECTION, query, mode, pool)

    hits = []
    for mid, relevance, info in ranking.hits:
        m = materials.get(mid)
        if m is None or not matches_filters(m, filters):
            continue
        hits.append({**m.summary(), "text_html": highlight(m.short_text, ranking.matched.get(mid, set())),
                     "relevance": relevance, **info})
    return {
        "query": query,
        "mode": mode,
        "total": len(hits),
        "hits": hits[:min(limit, MAX_RESULTS)],
        "did_you_mean": ranking.did_you_mean,
        "expansions": ranking.expansions,
        "warnings": ranking.warnings,
    }


def suggest(prefix: str, limit: int = 8) -> list[dict]:
    """Search-as-you-type: lexical only (no embedding call per keystroke)."""
    materials = load_materials()
    idx = index()
    docs, _, _ = idx.search(prefix, limit * 3)
    exact = [doc for doc, _ in idx.exact(prefix)]
    result, seen = [], set()
    for doc in exact + [d for d, _ in docs]:
        m = materials[idx.ids[doc]]
        if m.short_text and m.short_text not in seen and not m.deleted:
            seen.add(m.short_text)
            result.append({"matnr": m.id, "text": m.short_text})
        if len(result) >= limit:
            break
    return result


def facets() -> dict:
    """Filter values with their number of materials."""
    counts = {"mtart": Counter(), "matkl": Counter(), "vendor": Counter()}
    for m in load_materials().values():
        counts["mtart"][m.mara.get("MTART", "")] += 1
        counts["matkl"][m.mara.get("MATKL", "")] += 1
        counts["vendor"].update(m.vendors)
    return {k: textsearch.facet_list(c) for k, c in counts.items()}
