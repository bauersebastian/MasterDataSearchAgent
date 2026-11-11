"""Hybrid business partner search: exact keys (partner / customer / vendor number, tax number, IBAN, e-mail, phone,
...), fuzzy text search over names, addresses and communication data and semantic search, fused with RRF."""
from collections import Counter
from functools import lru_cache
from typing import Optional

from .. import textsearch
from ..config import CANDIDATES, MAX_RESULTS, PARTNER_COLLECTION
from ..textsearch import LexicalIndex, highlight
from .data import COUNTRIES, TYPES, Partner, load_partners

FILTERS = ("type", "role", "country", "group")


@lru_cache(maxsize=1)
def index() -> LexicalIndex:
    return LexicalIndex(load_partners())


def matches_filters(p: Partner, filters: dict) -> bool:
    if not filters.get("include_deleted") and p.deleted:
        return False
    if filters.get("type") and p.type not in filters["type"]:
        return False
    if filters.get("role") and not set(filters["role"]) & set(p.roles):
        return False
    if filters.get("country") and p.address.get("country", "") not in filters["country"]:
        return False
    if filters.get("group") and p.g000.get("BU_GROUP") not in filters["group"]:
        return False
    return True


def hit(p: Partner, terms: set) -> dict:
    return {**p.summary(), "name_html": highlight(p.display_name, terms),
            "address_html": highlight(p.address.get("text", ""), terms)}


def search(query: str, mode: str = "hybrid", filters: Optional[dict] = None, limit: int = 50) -> dict:
    """mode: hybrid | semantic | lexical. Returns ranked hits with the score of each retriever."""
    filters = filters or {}
    partners = load_partners()
    query = query.strip()
    pool = CANDIDATES * (5 if any(filters.get(k) for k in FILTERS) else 1)
    ranking = textsearch.rank(index(), PARTNER_COLLECTION, query, mode, pool)

    hits = []
    for pid, relevance, info in ranking.hits:
        p = partners.get(pid)
        if p is None or not matches_filters(p, filters):
            continue
        hits.append({**hit(p, ranking.matched.get(pid, set())), "relevance": relevance, **info})
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
    partners = load_partners()
    idx = index()
    docs, _, _ = idx.search(prefix, limit * 3)
    exact = [doc for doc, _ in idx.exact(prefix)]
    result, seen = [], set()
    for doc in exact + [d for d, _ in docs]:
        p = partners[idx.ids[doc]]
        if p.id not in seen and not p.deleted:
            seen.add(p.id)
            place = ", ".join(x for x in (p.address.get("city"), p.address.get("country")) if x)
            result.append({"partner": p.id, "text": p.display_name, "description": f"{p.id} · {place}" if place else p.id})
        if len(result) >= limit:
            break
    return result


def facets() -> dict:
    """Filter values with their number of partners."""
    counts = {k: Counter() for k in FILTERS}
    for p in load_partners().values():
        counts["type"][p.type] += 1
        counts["role"].update(p.roles)
        counts["country"][p.address.get("country", "")] += 1
        counts["group"][p.g000.get("BU_GROUP", "")] += 1
    labels = {"type": TYPES, "country": COUNTRIES}
    return {k: textsearch.facet_list(c, labels.get(k)) for k, c in counts.items()}
