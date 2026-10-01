"""Duplicate check for one material: candidates from semantic neighbours, fuzzy name similarity and shared keys
(EAN, vendor part number); optional assessment of the candidates by the LLM."""
import json
from collections import defaultdict
from functools import lru_cache
from typing import Literal, Optional

from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from . import vectordb
from .config import DUPLICATE_CANDIDATES, MODEL, REASONING_EFFORT, RRF_K
from .data import Material, load_materials
from .search import index, normalize


@lru_cache(maxsize=1)
def short_texts() -> tuple[list[str], list[str]]:
    """(material ids, normalized short texts) for the fuzzy name comparison."""
    materials = load_materials()
    return list(materials), [normalize(m.short_text) for m in materials.values()]


def semantic_neighbours(m: Material, limit: int) -> list[tuple[str, float]]:
    col = vectordb.collection()
    if not col.count():
        return []
    own = col.get(ids=[m.id], include=["embeddings"])
    if not len(own["ids"]):
        return []
    result = col.query(query_embeddings=[own["embeddings"][0]], n_results=limit + 1, include=["distances"])
    return [(i, 1 - d) for i, d in zip(result["ids"][0], result["distances"][0]) if i != m.id]


def candidates(matnr: str, limit: int = DUPLICATE_CANDIDATES) -> dict:
    materials = load_materials()
    m = materials.get(matnr)
    if m is None:
        raise KeyError(matnr)
    idx = index()
    pool = limit * 3

    fused: dict[str, float] = defaultdict(float)
    signals: dict[str, dict] = defaultdict(dict)

    for rank, (mid, sim) in enumerate(semantic_neighbours(m, pool)):
        fused[mid] += 1 / (RRF_K + rank + 1)
        signals[mid]["semantic"] = round(sim, 3)

    ids, texts = short_texts()
    if m.short_text:
        names = process.extract(normalize(m.short_text), texts, scorer=fuzz.token_set_ratio, limit=pool + 1)
        for rank, (_, score, pos) in enumerate(n for n in names if ids[n[2]] != m.id):
            fused[ids[pos]] += 1 / (RRF_K + rank + 1)
        lexical, _, _ = idx.search(m.short_text, pool + 1)
        for rank, (doc, score) in enumerate(d for d in lexical if idx.ids[d[0]] != m.id):
            fused[idx.ids[doc]] += 1 / (RRF_K + rank + 1)
            signals[idx.ids[doc]]["lexical"] = round(score, 3)

    # identical keys are the strongest duplicate indicator
    for key, label in [(e, "EAN") for e in m.eans] + [(p, "Lieferanten-Teilenr.") for p in m.vendor_parts]:
        for doc, found_as in idx.exact(key):
            mid = idx.ids[doc]
            if mid != m.id and found_as == label:
                fused[mid] += 1
                signals[mid].setdefault("shared", []).append(f"{label} {key}")

    result = []
    for mid, _ in sorted(fused.items(), key=lambda x: -x[1])[:limit]:
        c = materials[mid]
        s = signals[mid]
        name = fuzz.token_sort_ratio(normalize(m.short_text), normalize(c.short_text)) / 100
        score = max(name, s.get("semantic", 0))
        if s.get("shared"):
            score = max(score, 0.95)
        result.append({**c.summary(), "name_similarity": round(name, 3), "semantic": s.get("semantic"),
                       "lexical": s.get("lexical"), "shared": s.get("shared", []),
                       "same_unit": c.mara.get("MEINS") == m.mara.get("MEINS"),
                       "score": round(score * 100)})
    result.sort(key=lambda c: -c["score"])
    return {"material": m.summary(), "candidates": result, "semantic_available": vectordb.indexed_count() > 0}


# --- LLM assessment -------------------------------------------------------

class Verdict(BaseModel):
    matnr: str
    verdict: Literal["Dublette", "Mögliche Dublette", "Variante", "Verschieden"] = Field(
        description="Dublette: same product; Variante: same product family, but different size/type/colour/version; "
                    "Mögliche Dublette: probably the same product, but the data is not conclusive")
    confidence: float = Field(description="Confidence of the verdict between 0 and 1")
    reason: str = Field(description="Short reason in German, naming the decisive attributes")


class DuplicateAssessment(BaseModel):
    summary: str = Field(description="One or two sentences in German: is the material a duplicate, and of which?")
    verdicts: list[Verdict]


SYSTEM_PROMPT = """You are an SAP material master data steward checking for duplicate material master records.
Compare the reference material with each candidate and decide whether they describe the same product.

Rules:
- Identical EAN/GTIN or identical manufacturer/vendor part number of the same product is strong evidence for a duplicate,
  but internal in-store EANs (prefix 20-29) may be reused and are weaker evidence.
- Different article numbers, sizes, threads (M20 vs M25), lengths, colours, wattages, light colours (830 vs 840)
  or pack quantities mean different products, even if the texts are almost identical: verdict "Variante".
- Texts in different languages or with abbreviations ("Verschr." = "Verschraubung", "Ms" = "Messing") can describe the
  same product.
- Test or placeholder materials ("test", "Material 40") are only duplicates if the data is really identical.
- Base your verdict only on the given data. Reply in German.
"""


def compact(m: Material) -> dict:
    """The attributes of a material that matter for the comparison."""
    mara = m.mara
    data = {
        "matnr": m.id, "texts": m.texts if len(m.texts) <= 6 else {k: m.texts[k] for k in list(m.texts)[:6]},
        "material_type": mara.get("MTART"), "material_group": mara.get("MATKL"), "base_unit": mara.get("MEINS"),
        "eans": m.eans, "old_material_number": mara.get("BISMT"),
        "gross_weight": f"{mara.get('BRGEW')} {mara.get('GEWEI')}" if mara.get("BRGEW") else None,
        "net_weight": f"{mara.get('NTGEW')} {mara.get('GEWEI')}" if mara.get("NTGEW") else None,
        "dimensions": (f"{mara.get('LAENG')} x {mara.get('BREIT')} x {mara.get('HOEHE')} {mara.get('MEABM')}"
                       if mara.get("LAENG") else None),
        "units": [f"{r['MEINH']} = {r['UMREZ']}/{r['UMREN']} {mara.get('MEINS')}" for r in m.marm],
        "vendors": [{"vendor": r["LIFNR"].lstrip("0"), "vendor_part_number": r.get("IDNLF")} for r in m.eina[:5]],
        "classes": m.classes, "features": dict(m.features()),
        "long_texts": {k: v[:500] for k, v in m.long_texts().items()},
        "deleted": m.deleted,
    }
    return {k: v for k, v in data.items() if v not in (None, "", [], {})}


def assess(matnr: str, candidate_ids: Optional[list[str]] = None) -> dict:
    """Let the LLM judge the candidates (default: the heuristic candidates)."""
    materials = load_materials()
    if matnr not in materials:
        raise KeyError(matnr)
    if not candidate_ids:
        candidate_ids = [c["matnr"] for c in candidates(matnr)["candidates"]]
    candidate_ids = [c for c in candidate_ids if c in materials and c != matnr][:DUPLICATE_CANDIDATES]
    if not candidate_ids:
        return {"summary": "Keine Kandidaten gefunden.", "verdicts": []}

    user = ("Reference material:\n" + json.dumps(compact(materials[matnr]), ensure_ascii=False)
            + "\n\nCandidates:\n" + "\n".join(json.dumps(compact(materials[c]), ensure_ascii=False)
                                              for c in candidate_ids))
    response = vectordb.client().responses.parse(
        model=MODEL,
        reasoning={"effort": REASONING_EFFORT},
        input=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        text_format=DuplicateAssessment,
    )
    result = response.output_parsed
    if result is None:
        raise RuntimeError(f"Das LLM hat kein strukturiertes Ergebnis geliefert: {response.output_text!r}")
    verdicts = [v for v in result.verdicts if v.matnr in candidate_ids]
    return {"summary": result.summary, "verdicts": [v.model_dump() for v in verdicts], "model": MODEL}
