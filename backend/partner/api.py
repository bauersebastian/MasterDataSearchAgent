"""REST API of the business partner search (/api/partners/...)."""
import os
from typing import Literal

import openai
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..errors import openai_error
from . import ai_search, search
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


@router.get("/{partner}")
def get_partner(partner: str) -> dict:
    p = load_partners().get(partner.lstrip("0") or partner)
    if p is None:
        raise HTTPException(404, f"Geschäftspartner {partner} nicht gefunden")
    return p.detail()
