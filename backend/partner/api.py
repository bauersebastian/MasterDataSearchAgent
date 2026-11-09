"""REST API of the business partner search (/api/partners/...)."""
import os
from typing import Literal, Optional

import openai
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..errors import openai_error
from . import ai_search, deletion_check, duplicates, sap, search
from .data import load_partners, table_labels

router = APIRouter(prefix="/api/partners")


class SearchRequest(BaseModel):
    query: str
    mode: Literal["hybrid", "semantic", "lexical", "ai"] = "hybrid"
    type: list[str] = []
    role: list[str] = []
    country: list[str] = []
    group: list[str] = []
    include_deleted: bool = False
    limit: int = 50


class AssessRequest(BaseModel):
    candidates: Optional[list[str]] = None


@router.post("/search")
def run_search(req: SearchRequest) -> dict:
    if not req.query.strip():
        raise HTTPException(400, "Bitte einen Suchbegriff eingeben")
    if req.mode != "lexical" and not os.getenv("OPENAI_API_KEY"):
        if req.mode in ("semantic", "ai"):
            raise HTTPException(500, "OPENAI_API_KEY ist nicht konfiguriert - semantische Suche und KI-Suche nicht möglich")
        req.mode = "lexical"
    filters = req.model_dump(include={"type", "role", "country", "group", "include_deleted"})
    try:
        if req.mode == "ai":
            return ai_search.ai_search(req.query, filters)
        return search.search(req.query, req.mode, filters, req.limit)
    except openai.OpenAIError as e:
        raise openai_error(e) from e


@router.get("/suggest")
def get_suggestions(q: str = Query(..., min_length=1)) -> list[dict]:
    return search.suggest(q)


@router.get("/tables")
def get_table_labels() -> dict[str, str]:
    """GPA table name -> description (objects.md)."""
    return table_labels()


@router.get("/duplicates")
def get_duplicate_overview(max_tier: int = Query(3, ge=1, le=4), include_deleted: bool = False,
                           refresh: bool = False) -> dict:
    """Groups of probable duplicates over all partners (heuristic, no LLM)."""
    try:
        return duplicates.overview(max_tier, include_deleted, refresh)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e


@router.get("/{partner}")
def get_partner(partner: str) -> dict:
    p = load_partners().get(partner.lstrip("0") or partner)
    if p is None:
        raise HTTPException(404, f"Geschäftspartner {partner} nicht gefunden")
    return p.detail()


@router.get("/{partner}/xml")
def get_xml(partner: str) -> dict:
    """SOAP message of the partner for the DXTO import (sent with POST /api/sap/post like a material)."""
    p = load_partners().get(partner.lstrip("0") or partner)
    if p is None:
        raise HTTPException(404, f"Geschäftspartner {partner} nicht gefunden")
    return sap.message(p)


@router.post("/{partner}/deletion-check")
def run_deletion_check(partner: str) -> dict:
    """Open documents in SAP that block deleting the partner, read live on demand."""
    p = load_partners().get(partner.lstrip("0") or partner)
    if p is None:
        raise HTTPException(404, f"Geschäftspartner {partner} nicht gefunden")
    if not (os.getenv("SAP_USER") and os.getenv("SAP_PASSWORD")):
        raise HTTPException(500, "SAP_USER / SAP_PASSWORD sind auf dem Server nicht konfiguriert")
    return deletion_check.check(p)


@router.get("/{partner}/duplicates")
def get_duplicates(partner: str) -> dict:
    try:
        return duplicates.candidates(partner.lstrip("0") or partner)
    except KeyError as e:
        raise HTTPException(404, f"Geschäftspartner {partner} nicht gefunden") from e
    except openai.OpenAIError as e:
        raise openai_error(e) from e


@router.post("/{partner}/duplicates/assess")
def assess_duplicates(partner: str, req: AssessRequest) -> dict:
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(500, "OPENAI_API_KEY ist nicht konfiguriert - keine KI-Bewertung möglich")
    try:
        return duplicates.assess(partner.lstrip("0") or partner, req.candidates)
    except KeyError as e:
        raise HTTPException(404, f"Geschäftspartner {partner} nicht gefunden") from e
    except openai.OpenAIError as e:
        raise openai_error(e) from e
