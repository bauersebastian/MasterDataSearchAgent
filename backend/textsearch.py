"""Search engine shared by all master data objects: exact keys, probabilistic lexical matching (BM25 with fuzzy /
prefix / compound term expansion) and semantic vector search (Chroma), combined to one weighted score.

A searchable object provides `id`, `search_text()` (everything a user may type) and `search_keys()`
([(label, [values])] for exact matches of numbers and codes)."""
import html
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional, Protocol

from rapidfuzz import fuzz, process

from . import vectordb
from .config import (CANDIDATES, FUZZY_CUTOFF, HYBRID_SEMANTIC_WEIGHT, MIN_SEMANTIC_SIMILARITY,
                     SEMANTIC_FULL_SIMILARITY)

K1, B = 1.2, 0.75           # BM25 parameters
FOLD = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "é": "e", "è": "e", "à": "a", "ç": "c"})

# weight of a query term match by kind (exact > prefix > compound part > typo)
WEIGHTS = {"exact": 1.0, "prefix": 0.85, "compound": 0.7, "fuzzy": 0.8}


class Searchable(Protocol):
    id: str

    def search_text(self) -> str: ...

    def search_keys(self) -> list[tuple[str, list[str]]]: ...


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
    """BM25 over all searchable texts of an object, with typo-tolerant term expansion."""

    def __init__(self, objects: dict[str, Searchable]):
        self.ids = list(objects)
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lengths = []
        self.keys: dict[str, list[tuple[int, str]]] = defaultdict(list)   # compact key -> (doc, field label)
        for doc, o in enumerate(objects.values()):
            tokens = tokenize(o.search_text())
            self.lengths.append(len(tokens))
            for term, tf in Counter(tokens).items():
                self.postings[term].append((doc, tf))
            for label, values in o.search_keys():
                for value in values:
                    if (key := compact(value)) and (doc, label) not in self.keys[key]:
                        self.keys[key].append((doc, label))
        self.pos = {oid: doc for doc, oid in enumerate(self.ids)}
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
        if key.isdigit():   # numbers are also found with leading zeros
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


def highlight(text: str, terms: set[str]) -> str:
    """Text as HTML (for sap.m.FormattedText) with the words that matched the query in bold."""
    out = []
    for part in re.split(r"(\W+)", text):
        tokens = set(tokenize(part)) if part.strip() else set()
        escaped = html.escape(part)
        out.append(f"<strong>{escaped}</strong>" if tokens & terms else escaped)
    return "".join(out)


@dataclass
class Ranking:
    """Fused ranking of one query: ids best first with the display relevance and the score of each retriever."""
    hits: list[tuple[str, int, dict]]      # (id, relevance 0-100, {"exact": label, "lexical": .., "semantic": ..})
    matched: dict[str, set]                # vocabulary terms that matched per id (for highlighting)
    did_you_mean: Optional[str]
    expansions: list[dict]
    warnings: list[str]


def calibrate(similarity: float) -> float:
    """Cosine similarity -> 0..1: MIN_SEMANTIC_SIMILARITY (unrelated) is 0, SEMANTIC_FULL_SIMILARITY and above 1."""
    span = SEMANTIC_FULL_SIMILARITY - MIN_SEMANTIC_SIMILARITY
    return min(max((similarity - MIN_SEMANTIC_SIMILARITY) / span, 0.0), 1.0)


def rank(idx: LexicalIndex, collection: str, query: str, mode: str, pool: int) -> Ranking:
    """mode: hybrid | semantic | lexical. Exact key hits always come first.

    Hybrid: the candidates of both retrievers are scored by both. Lexical hits that the (approximate) vector search
    did not return get their exact similarity from the stored embeddings. The score is the weighted sum of the
    lexical score and the calibrated similarity; without any lexical hit only the similarity counts."""
    query = query.strip()
    warnings = []
    exact = [(idx.ids[doc], label) for doc, label in idx.exact(query)]
    lexical, matched, per_token = ([], {}, [])
    if mode in ("hybrid", "lexical"):
        lexical, matched, per_token = idx.search(query, pool)
    lex = {idx.ids[doc]: score for doc, score in lexical}
    sem: dict[str, float] = {}
    if mode in ("hybrid", "semantic"):
        if vectordb.indexed_count(collection):
            # ids missing in the lexical index: vector index older than the input files
            sem = {oid: sim for oid, sim in vectordb.query(collection, query, pool) if oid in idx.pos}
            if mode == "hybrid":
                exact_ids = {oid for oid, _ in exact}   # a similarity to a number or code says nothing
                missing = [oid for oid in lex if oid not in sem and oid not in exact_ids]
                sem.update(vectordb.similarities(collection, query, missing))
        else:
            warnings.append("Der Vektorindex ist leer - semantische Suche nicht verfügbar "
                            "(python -m backend.indexer ausführen)")

    weight = HYBRID_SEMANTIC_WEIGHT if lex else 1.0

    def score(oid: str) -> float:
        if mode == "lexical":
            return lex.get(oid, 0.0)
        if mode == "semantic":
            return sem.get(oid, 0.0)
        return (1 - weight) * lex.get(oid, 0.0) + weight * calibrate(sem.get(oid, 0.0))

    exact_rank = {}
    for r, (oid, label) in enumerate(exact):
        exact_rank.setdefault(oid, (r, label))
    candidates = list(dict.fromkeys([*exact_rank, *lex, *(sem if mode != "lexical" else ())]))
    scores = {oid: score(oid) for oid in candidates}
    # exact key hits first, then by score; equal scores keep the order lexical > semantic > id (deterministic)
    order = sorted(candidates, key=lambda oid: (exact_rank.get(oid, (math.inf,))[0], -round(scores[oid], 6),
                                                -lex.get(oid, 0.0), -sem.get(oid, 0.0), oid))

    def info(oid: str) -> dict:
        s = sem.get(oid)
        return {"exact": exact_rank[oid][1] if oid in exact_rank else None,
                "lexical": round(lex[oid], 3) if oid in lex else None,
                "semantic": round(s, 3) if s is not None else None}

    hits = [(oid, 100 if oid in exact_rank else round(min(scores[oid], 1) * 100), info(oid)) for oid in order]
    return Ranking(
        hits=hits,
        matched={idx.ids[doc]: terms for doc, terms in matched.items()},
        did_you_mean=idx.did_you_mean(per_token),
        expansions=[{"token": t, "terms": [{"term": m.term, "kind": m.kind} for m in ex[:8]]} for t, ex in per_token],
        warnings=warnings,
    )


def facet_list(counter: Counter, labels: Optional[dict] = None) -> list[dict]:
    """Filter values with their number of objects, most frequent first."""
    labels = labels or {}
    return [{"key": v, "text": labels.get(v, v), "count": n}
            for v, n in sorted(counter.items(), key=lambda x: (-x[1], x[0])) if v]
