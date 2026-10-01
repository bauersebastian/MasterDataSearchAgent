"""Hybrid material search: exact keys, probabilistic lexical matching (BM25 with fuzzy / prefix / compound term
expansion) and semantic vector search (Chroma), combined with Reciprocal Rank Fusion."""
import html
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from rapidfuzz import fuzz, process

from . import vectordb
from .config import CANDIDATES, FUZZY_CUTOFF, MAX_RESULTS, MIN_SEMANTIC_SIMILARITY, RRF_K
from .data import Material, load_materials

K1, B = 1.2, 0.75           # BM25 parameters
FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "é": "e", "è": "e", "à": "a", "ç": "c"})

# weight of a query term match by kind (exact > prefix > compound part > typo)
WEIGHTS = {"exact": 1.0, "prefix": 0.85, "compound": 0.7, "fuzzy": 0.8}


def normalize(text: str) -> str:
    return text.lower().translate(FOLD)


def tokenize(text: str) -> list[str]:
    """Lower case, umlauts folded; mixed tokens like 'm25' or '6x45' also yield their letter / digit parts."""
    tokens = []
    for token in re.findall(r"[a-z0-9]+", normalize(text)):
        tokens.append(token)
        parts = re.findall(r"[a-z]+|[0-9]+", token)
        if len(parts) > 1:
            tokens.extend(parts)
    return tokens


def compact(text: str) -> str:
    """Key form for exact matches of numbers / codes: '4021598-024366' == '4021598024366'."""
    return re.sub(r"[^a-z0-9]", "", normalize(text))


@dataclass
class TermMatch:
    term: str       # vocabulary term
    kind: str       # exact / prefix / compound / fuzzy
    weight: float


class LexicalIndex:
    """BM25 over all searchable texts of a material, with typo-tolerant term expansion."""

    def __init__(self, materials: dict[str, Material]):
        self.ids = list(materials)
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lengths = []
        self.keys: dict[str, list[tuple[int, str]]] = defaultdict(list)   # compact key -> (doc, field label)
        for doc, m in enumerate(materials.values()):
            tokens = tokenize(m.search_text())
            self.lengths.append(len(tokens))
            for term, tf in Counter(tokens).items():
                self.postings[term].append((doc, tf))
            for label, values in (("Materialnummer", [m.id, m.matnr]), ("EAN", m.eans),
                                  ("Lieferanten-Teilenr.", m.vendor_parts), ("Alte Materialnr.", [m.mara.get("BISMT", "")])):
                for value in values:
                    if key := compact(value):
                        self.keys[key].append((doc, label))
        self.pos = {mid: doc for doc, mid in enumerate(self.ids)}
        self.n = len(self.ids)
        self.avg_len = sum(self.lengths) / max(self.n, 1)
        self.vocab = list(self.postings)
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def expand(self, token: str) -> list[TermMatch]:
        """Vocabulary terms that a query token may stand for."""
        matches = {}
        if token in self.postings:
            matches[token] = TermMatch(token, "exact", WEIGHTS["exact"])
        if len(token) >= 3 and not token.isdigit():
            for term in self.vocab:
                if term not in matches and term.startswith(token):
                    matches[term] = TermMatch(term, "prefix", WEIGHTS["prefix"])
                elif len(token) >= 5 and term not in matches and token in term:
                    matches[term] = TermMatch(term, "compound", WEIGHTS["compound"])   # 'mutter' in 'gegenmutter'
        if len(token) >= 4 and token.isalpha():   # no typo correction for codes like '6x45' or 'm25'
            for term, score, _ in process.extract(token, self.vocab, scorer=fuzz.ratio, score_cutoff=FUZZY_CUTOFF,
                                                  limit=8):
                if term not in matches:
                    matches[term] = TermMatch(term, "fuzzy", WEIGHTS["fuzzy"] * score / 100)
        # keep the best expansions only (common prefixes like 'ver' would otherwise match thousands of terms)
        return sorted(matches.values(), key=lambda m: (-m.weight, -len(self.postings[m.term])))[:40]

    def search(self, query: str, limit: int = CANDIDATES) -> tuple[list[tuple[int, float]], dict[int, set], list]:
        """[(doc, score 0..1)], matched vocabulary terms per doc, expansions per query token."""
        q_tokens = []
        for token in re.findall(r"[a-z0-9]+", normalize(query)):
            parts = re.findall(r"[a-z]+|[0-9]+", token)
            # unknown codes like '6x45' are matched by their parts ('6 x 45' in the text)
            q_tokens += [token] if token in self.postings or len(parts) == 1 else parts
        q_tokens = list(dict.fromkeys(q_tokens))
        if not q_tokens:
            return [], {}, []
        per_token = []
        scores: dict[int, float] = defaultdict(float)
        hits: dict[int, int] = defaultdict(int)
        matched: dict[int, set] = defaultdict(set)
        max_score = 0.0
        for token in q_tokens:
            expansions = self.expand(token)
            per_token.append((token, expansions))
            best: dict[int, float] = {}
            # an expansion never weighs more than the word itself ('schraube' must not lose to 'schraubendreher')
            max_idf = self.idf.get(token, math.inf)
            for m in expansions:
                idf = min(self.idf[m.term], max_idf)
                for doc, tf in self.postings[m.term]:
                    norm = tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.lengths[doc] / self.avg_len))
                    s = idf * norm * m.weight
                    if s > best.get(doc, 0):
                        best[doc] = s
                    matched[doc].add(m.term)
            # 100 % = every query word found once in a document of average length
            max_score += max((min(self.idf[m.term], max_idf) for m in expansions), default=0)
            for doc, s in best.items():
                scores[doc] += s
                hits[doc] += 1
        # probabilistic AND: documents covering more query tokens are strongly preferred
        n = len(q_tokens)
        ranked = sorted(((doc, s * (hits[doc] / n) ** 2) for doc, s in scores.items()), key=lambda x: -x[1])[:limit]
        return [(doc, min(s / max_score, 1.0) if max_score else 0) for doc, s in ranked], matched, per_token

    def exact(self, query: str) -> list[tuple[int, str]]:
        key = compact(query)
        if len(key) < 2:
            return []
        hits = list(self.keys.get(key, []))
        if key.isdigit():   # material numbers are also found with leading zeros
            hits += [h for h in self.keys.get(key.lstrip("0"), []) if h not in hits]
        return hits

    def did_you_mean(self, per_token) -> Optional[str]:
        """Query with unknown tokens replaced by their closest vocabulary term."""
        changed, words = False, []
        for token, expansions in per_token:
            fuzzy = [m for m in expansions if m.kind == "fuzzy"]
            if not any(m.kind in ("exact", "prefix") for m in expansions) and fuzzy:
                words.append(max(fuzzy, key=lambda m: (m.weight, len(self.postings[m.term]))).term)
                changed = True
            else:
                words.append(token)
        return " ".join(words) if changed else None


@lru_cache(maxsize=1)
def index() -> LexicalIndex:
    return LexicalIndex(load_materials())


def highlight(text: str, terms: set[str]) -> str:
    """Short text as HTML (for sap.m.FormattedText) with the words that matched the query in bold."""
    out = []
    for part in re.split(r"(\W+)", text):
        tokens = set(tokenize(part)) if part.strip() else set()
        escaped = html.escape(part)
        out.append(f"<strong>{escaped}</strong>" if tokens & terms else escaped)
    return "".join(out)


def semantic(query: str, limit: int) -> list[tuple[str, float]]:
    """[(material id, cosine similarity)] from the Chroma DB."""
    col = vectordb.collection()
    count = col.count()
    if not count:
        return []
    result = col.query(query_embeddings=[list(vectordb.embed_query(query))], n_results=min(limit, count),
                       include=["distances"])
    return [(i, 1 - d) for i, d in zip(result["ids"][0], result["distances"][0]) if 1 - d >= MIN_SEMANTIC_SIMILARITY]


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


def relevance(mode: str, fused: float, info: dict) -> int:
    """0-100 for display: the retriever's own score in single modes, the normalized RRF score in hybrid mode."""
    if "exact" in info:
        return 100
    score = {"lexical": info.get("lexical"), "semantic": info.get("semantic")}.get(mode, fused)
    return round(min(score or 0, 1) * 100)


def search(query: str, mode: str = "hybrid", filters: Optional[dict] = None, limit: int = 50) -> dict:
    """mode: hybrid | semantic | lexical. Returns ranked hits with the score of each retriever."""
    filters = filters or {}
    materials = load_materials()
    idx = index()
    query = query.strip()
    warnings = []
    limit = min(limit, MAX_RESULTS)
    pool = CANDIDATES * (5 if any(filters.get(k) for k in ("mtart", "matkl", "vendor")) else 1)

    exact = [(idx.ids[doc], label) for doc, label in idx.exact(query)]
    lexical, matched, per_token = ([], {}, [])
    if mode in ("hybrid", "lexical"):
        lexical, matched, per_token = idx.search(query, pool)
    sem = []
    if mode in ("hybrid", "semantic"):
        if vectordb.indexed_count():
            sem = semantic(query, pool)
        else:
            warnings.append("Der Vektorindex ist leer - semantische Suche nicht verfügbar "
                            "(python -m backend.indexer ausführen)")

    # Reciprocal Rank Fusion; exact key hits always come first
    fused: dict[str, float] = defaultdict(float)
    info: dict[str, dict] = defaultdict(dict)
    for rank, (mid, label) in enumerate(exact):
        fused[mid] += 1.0 + 1 / (rank + 1)
        info[mid]["exact"] = label
    for rank, (doc, score) in enumerate(lexical):
        fused[idx.ids[doc]] += 1 / (RRF_K + rank + 1)
        info[idx.ids[doc]]["lexical"] = round(score, 3)
    for rank, (mid, sim) in enumerate(sem):
        fused[mid] += 1 / (RRF_K + rank + 1)
        info[mid]["semantic"] = round(sim, 3)
    best_possible = (2 if mode == "hybrid" else 1) / (RRF_K + 1)

    hits = []
    for mid, score in sorted(fused.items(), key=lambda x: -x[1]):
        m = materials.get(mid)
        if m is None or not matches_filters(m, filters):
            continue
        terms = matched.get(idx.pos[mid], set())
        hits.append({
            **m.summary(),
            "text_html": highlight(m.short_text, terms),
            "relevance": relevance(mode, score / best_possible, info[mid]),
            "exact": info[mid].get("exact"),
            "lexical": info[mid].get("lexical"),
            "semantic": info[mid].get("semantic"),
        })
    total = len(hits)
    return {
        "query": query,
        "mode": mode,
        "total": total,
        "hits": hits[:limit],
        "did_you_mean": idx.did_you_mean(per_token),
        "expansions": [{"token": t, "terms": [{"term": m.term, "kind": m.kind} for m in ex[:8]]}
                       for t, ex in per_token],
        "warnings": warnings,
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
    return {k: [{"key": v, "count": n} for v, n in sorted(c.items(), key=lambda x: (-x[1], x[0])) if v]
            for k, c in counts.items()}
