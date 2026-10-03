"""AI search: the LLM interprets a free-text request (what is searched, which requirements), the hybrid search
retrieves candidates, and the LLM checks every candidate against the requirements -- based only on the master data."""
import html
import json
from typing import Literal

from pydantic import BaseModel, Field

from . import search, vectordb
from .config import MODEL, REASONING_EFFORT
from .data import load_materials
from .duplicates import compact

CANDIDATES = 30   # candidates the LLM checks


class Requirement(BaseModel):
    text: str = Field(description="The requirement in short German words, e.g. 'temperaturbeständig bis mind. 120 °C'")


class Interpretation(BaseModel):
    product: str = Field(description="What kind of material is searched, in German, e.g. 'Dichtung für Geschirrspüler'")
    search_terms: str = Field(description="German keywords for a product search in SAP short texts: product type, "
                                          "synonyms, material, manufacturer, sizes -- no requirement wording like "
                                          "'mindestens' or 'geeignet', e.g. 'Dichtung Dichtring Silikon Geschirrspüler'")
    requirements: list[Requirement] = Field(description="Conditions the material must meet (properties, limits, "
                                                        "suitability); empty if the request only names a product")


class RequirementCheck(BaseModel):
    requirement: str = Field(description="The requirement text as given")
    status: Literal["belegt", "nicht belegt", "widerspricht"] = Field(
        description="belegt: the master data states that the requirement is met; widerspricht: the master data "
                    "states the opposite; nicht belegt: the master data says nothing about it")
    evidence: str = Field(description="The decisive master data value or text (field and value), empty if none")


class CandidateAssessment(BaseModel):
    matnr: str
    verdict: Literal["passend", "teilweise", "nicht passend"] = Field(
        description="passend: is the searched kind of product and no requirement is contradicted or unproven; "
                    "teilweise: is the searched kind of product, but requirements are not proven by the data; "
                    "nicht passend: other kind of product or a requirement is contradicted")
    score: int = Field(description="Relevance 0-100 for the request")
    reason: str = Field(description="One short German sentence")
    checks: list[RequirementCheck]


class Assessment(BaseModel):
    summary: str = Field(description="Two or three German sentences answering the request: which materials fit, "
                                     "which requirements are not documented in the master data; say plainly if "
                                     "nothing fits")
    candidates: list[CandidateAssessment]


INTERPRET_PROMPT = """You turn a user's free-text request into a structured product search over an SAP material
master (mostly German short texts, some long texts, classes and features). Reply in German."""

ASSESS_PROMPT = """You are an SAP master data expert. Check which of the candidate materials fit the user's request.

Rules:
- Use ONLY the given master data of each candidate (texts, long texts, classes, features, units, weights). Do not use
  general product knowledge, typical properties of materials or assumptions: a requirement is only "belegt" if the
  data states it, "widerspricht" only if the data states the opposite, otherwise "nicht belegt".
- Feature values like J / N mean yes / no.
- Judge first whether a candidate is the kind of product searched at all.
- It is fine if no candidate fits -- say so in the summary.
- Return an assessment for every candidate. Reply in German.
"""


def llm_parse(system: str, user: str, schema):
    response = vectordb.client().responses.parse(
        model=MODEL,
        reasoning={"effort": REASONING_EFFORT},
        input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        text_format=schema,
    )
    if response.output_parsed is None:
        raise RuntimeError(f"Das LLM hat kein strukturiertes Ergebnis geliefert: {response.output_text!r}")
    return response.output_parsed


def retrieve(query: str, interpretation: Interpretation, filters: dict) -> list[dict]:
    """Hybrid search with the extracted keywords, completed by the semantic hits of the whole request."""
    semantic = vectordb.indexed_count() > 0
    hits = search.search(interpretation.search_terms, "hybrid" if semantic else "lexical", filters, CANDIDATES)["hits"]
    if semantic and len(hits) < CANDIDATES:
        seen = {h["matnr"] for h in hits}
        extra = search.search(query, "semantic", filters, CANDIDATES)["hits"]
        hits += [h for h in extra if h["matnr"] not in seen][:CANDIDATES - len(hits)]
    return hits


def ai_search(query: str, filters: dict) -> dict:
    interpretation = llm_parse(INTERPRET_PROMPT, query, Interpretation)
    candidates = retrieve(query, interpretation, filters)
    result = {
        "query": query, "mode": "ai", "hits": [], "total": 0, "did_you_mean": None, "expansions": [], "warnings": [],
        "interpretation": interpretation.model_dump(), "candidates_checked": len(candidates), "model": MODEL,
    }
    if not vectordb.indexed_count():
        result["warnings"].append("Der Vektorindex ist leer - Kandidaten nur aus der unscharfen Textsuche")
    if not candidates:
        result["summary"] = "Zu dieser Anfrage wurden keine Kandidaten im Materialstamm gefunden."
        return result

    materials = load_materials()
    user = (f"Request: {query}\n\nSearched product: {interpretation.product}\nRequirements:\n"
            + "\n".join(f"- {r.text}" for r in interpretation.requirements) + "\n\nCandidates:\n"
            + "\n".join(json.dumps(compact(materials[h["matnr"]]), ensure_ascii=False) for h in candidates))
    assessment = llm_parse(ASSESS_PROMPT, user, Assessment)

    by_id = {h["matnr"]: h for h in candidates}
    hits = []
    for a in assessment.candidates:
        hit = by_id.get(a.matnr)
        if hit is None or a.verdict == "nicht passend":
            continue
        hits.append({**hit, "text_html": html.escape(hit["text"]), "relevance": max(0, min(a.score, 100)),
                     "ai_verdict": a.verdict, "ai_reason": a.reason,
                     "ai_checks": [c.model_dump() for c in a.checks]})
    hits.sort(key=lambda h: (h["ai_verdict"] != "passend", -h["relevance"]))
    result.update(hits=hits, total=len(hits), summary=assessment.summary)
    return result
