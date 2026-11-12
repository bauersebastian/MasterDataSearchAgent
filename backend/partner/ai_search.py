"""AI search for business partners: the LLM interprets a free-text request (which partner, which requirements), the
hybrid search retrieves candidates, and the LLM checks every candidate against the requirements -- based only on the
master data (names, roles, addresses, communication, contacts, customer / vendor data, materials supplied)."""
import html
import json
from typing import Literal

from pydantic import BaseModel, Field

from .. import llm, vectordb
from ..config import MODEL, PARTNER_COLLECTION
from . import search
from .data import load_partners

CANDIDATES = 30   # candidates the LLM checks


class Requirement(BaseModel):
    text: str = Field(description="The requirement in short German words, e.g. 'Sitz in Bayern'")


class Interpretation(BaseModel):
    product: str = Field(description="What kind of business partner is searched, in German, e.g. "
                                     "'Lieferant für Kabelverschraubungen' or 'Ansprechpartner im Einkauf bei BMW'")
    search_terms: str = Field(description="German keywords for a search over partner names, cities, industries and "
                                          "the materials a vendor supplies -- no requirement wording like 'mindestens', "
                                          "e.g. 'Kabelverschraubung Verschraubung Messing Lieferant'")
    requirements: list[Requirement] = Field(description="Conditions the partner must meet (role, country/region, "
                                                        "communication data, bank data, not blocked, ...); empty if "
                                                        "the request only names a partner")


class RequirementCheck(BaseModel):
    requirement: str = Field(description="The requirement text as given")
    status: Literal["belegt", "nicht belegt", "widerspricht"] = Field(
        description="belegt: the master data states that the requirement is met; widerspricht: the master data "
                    "states the opposite; nicht belegt: the master data says nothing about it")
    evidence: str = Field(description="The decisive master data value (field and value), empty if none")


class CandidateAssessment(BaseModel):
    partner: str = Field(description="Partner number of the candidate")
    verdict: Literal["passend", "teilweise", "nicht passend"] = Field(
        description="passend: is the searched kind of partner and no requirement is contradicted or unproven; "
                    "teilweise: is the searched kind of partner, but requirements are not proven by the data; "
                    "nicht passend: other kind of partner or a requirement is contradicted")
    score: int = Field(description="Relevance 0-100 for the request")
    reason: str = Field(description="One short German sentence")
    checks: list[RequirementCheck]


class Assessment(BaseModel):
    summary: str = Field(description="Two or three German sentences answering the request: which partners fit, "
                                     "which requirements are not documented in the master data; say plainly if "
                                     "nothing fits")
    candidates: list[CandidateAssessment]


INTERPRET_PROMPT = """You turn a user's free-text request into a structured search over SAP business partners
(organisations, persons as contact persons, groups; roles customer / vendor; addresses; contact persons; for vendors
the materials they supply). Reply in German."""

ASSESS_PROMPT = """You are an SAP master data expert. Check which of the candidate business partners fit the user's
request.

Rules:
- Use ONLY the given master data of each candidate (names, roles, addresses, communication, relations / contact
  persons, customer and vendor data, materials supplied, blocks). Do not use general knowledge about companies:
  a requirement is only "belegt" if the data states it, "widerspricht" only if the data states the opposite,
  otherwise "nicht belegt".
- A region can be derived from the postcode and city of the address (e.g. 97xxx Würzburg -> Bayern / Unterfranken).
- Judge first whether a candidate is the kind of partner searched at all (role, organisation vs. person).
- It is fine if no candidate fits -- say so in the summary.
- Return an assessment for every candidate. Reply in German.
"""


def retrieve(query: str, interpretation: Interpretation, filters: dict) -> list[dict]:
    """Hybrid search with the extracted keywords, completed by the semantic hits of the whole request."""
    semantic = vectordb.indexed_count(PARTNER_COLLECTION) > 0
    hits = search.search(interpretation.search_terms, "hybrid" if semantic else "lexical", filters, CANDIDATES)["hits"]
    if semantic and len(hits) < CANDIDATES:
        seen = {h["partner"] for h in hits}
        extra = search.search(query, "semantic", filters, CANDIDATES)["hits"]
        hits += [h for h in extra if h["partner"] not in seen][:CANDIDATES - len(hits)]
    return hits


def ai_search(query: str, filters: dict) -> dict:
    interpretation = llm.parse(INTERPRET_PROMPT, query, Interpretation, "partner-ai-search")
    candidates = retrieve(query, interpretation, filters)
    result = {
        "query": query, "mode": "ai", "hits": [], "total": 0, "did_you_mean": None, "expansions": [], "warnings": [],
        "interpretation": interpretation.model_dump(), "candidates_checked": len(candidates), "model": MODEL,
    }
    if not vectordb.indexed_count(PARTNER_COLLECTION):
        result["warnings"].append("Der Vektorindex ist leer - Kandidaten nur aus der unscharfen Textsuche")
    if not candidates:
        result["summary"] = "Zu dieser Anfrage wurden keine Kandidaten im Geschäftspartnerstamm gefunden."
        return result

    partners = load_partners()
    user = (f"Request: {query}\n\nSearched partner: {interpretation.product}\nRequirements:\n"
            + "\n".join(f"- {r.text}" for r in interpretation.requirements) + "\n\nCandidates:\n"
            + "\n".join(json.dumps(partners[h["partner"]].llm_view(), ensure_ascii=False) for h in candidates))
    assessment = llm.parse(ASSESS_PROMPT, user, Assessment, "partner-ai-search")

    by_id = {h["partner"]: h for h in candidates}
    hits = []
    for a in assessment.candidates:
        hit = by_id.get(a.partner.lstrip("0") or a.partner)
        if hit is None or a.verdict == "nicht passend":
            continue
        hits.append({**hit, "name_html": html.escape(hit["name"]), "relevance": max(0, min(a.score, 100)),
                     "ai_verdict": a.verdict, "ai_reason": a.reason,
                     "ai_checks": [c.model_dump() for c in a.checks]})
    hits.sort(key=lambda h: (h["ai_verdict"] != "passend", -h["relevance"]))
    result.update(hits=hits, total=len(hits), summary=assessment.summary)
    return result
