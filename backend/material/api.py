"""REST API of the material master search (paths unchanged: /api/search, /api/materials/...)."""
import logging
import os
import xml.etree.ElementTree as ET
from typing import Literal, Optional

import openai
import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..errors import openai_error
from . import ai_search, bom, duplicates, sap, search
from .data import load_materials

log = logging.getLogger("mdsa")
router = APIRouter(prefix="/api")


class SearchRequest(BaseModel):
    query: str
    mode: Literal["hybrid", "semantic", "lexical", "ai"] = "hybrid"
    mtart: list[str] = []
    matkl: list[str] = []
    vendor: list[str] = []
    include_deleted: bool = False
    limit: int = 50


class AssessRequest(BaseModel):
    candidates: Optional[list[str]] = None


class PostRequest(BaseModel):
    xml: str


# plain `def` endpoints run in FastAPI's thread pool, so OpenAI calls do not block other requests
@router.post("/search")
def run_search(req: SearchRequest) -> dict:
    if not req.query.strip():
        raise HTTPException(400, "Bitte einen Suchbegriff eingeben")
    if req.mode != "lexical" and not os.getenv("OPENAI_API_KEY"):
        if req.mode in ("semantic", "ai"):
            raise HTTPException(500, "OPENAI_API_KEY ist nicht konfiguriert - semantische Suche und KI-Suche nicht möglich")
        req.mode = "lexical"
    filters = req.model_dump(include={"mtart", "matkl", "vendor", "include_deleted"})
    try:
        if req.mode == "ai":
            return ai_search.ai_search(req.query, filters)
        return search.search(req.query, req.mode, filters, req.limit)
    except openai.OpenAIError as e:
        raise openai_error(e) from e


@router.get("/suggest")
def get_suggestions(q: str = Query(..., min_length=1)) -> list[dict]:
    return search.suggest(q)


def vendor_partners() -> dict[str, str]:
    """Vendor number -> business partner number (link from the purchasing info records to the partner)."""
    try:
        from ..partner.data import load_partners
        return {p.lifnr: p.id for p in load_partners().values() if p.lifnr}
    except FileNotFoundError:
        return {}


@router.get("/materials/{matnr}")
def get_material(matnr: str) -> dict:
    m = load_materials().get(matnr)
    if m is None:
        raise HTTPException(404, f"Material {matnr} nicht gefunden")
    detail = m.detail()
    partners = vendor_partners()
    detail["eina"] = [{**r, "partner": partners.get(r["LIFNR"], "")} for r in detail["eina"]]
    return detail


@router.get("/materials/{matnr}/bom")
def get_bom(matnr: str, stlan: str = "", werks: str = "") -> dict:
    if matnr not in load_materials():
        raise HTTPException(404, f"Material {matnr} nicht gefunden")
    return bom.structure(matnr, stlan, werks)


@router.get("/materials/{matnr}/duplicates")
def get_duplicates(matnr: str) -> dict:
    try:
        return duplicates.candidates(matnr)
    except KeyError as e:
        raise HTTPException(404, f"Material {matnr} nicht gefunden") from e
    except openai.OpenAIError as e:
        raise openai_error(e) from e


@router.post("/materials/{matnr}/duplicates/assess")
def assess_duplicates(matnr: str, req: AssessRequest) -> dict:
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(500, "OPENAI_API_KEY ist nicht konfiguriert - keine KI-Bewertung möglich")
    try:
        return duplicates.assess(matnr, req.candidates)
    except KeyError as e:
        raise HTTPException(404, f"Material {matnr} nicht gefunden") from e
    except openai.OpenAIError as e:
        raise openai_error(e) from e


@router.get("/materials/{matnr}/xml")
def get_xml(matnr: str) -> dict:
    m = load_materials().get(matnr)
    if m is None:
        raise HTTPException(404, f"Material {matnr} nicht gefunden")
    return sap.message(m)


@router.post("/sap/post")
def post_to_sap(req: PostRequest) -> dict:
    if not (os.getenv("SAP_USER") and os.getenv("SAP_PASSWORD")):
        raise HTTPException(500, "SAP_USER / SAP_PASSWORD sind auf dem Server nicht konfiguriert")
    try:
        ET.fromstring(req.xml.encode("utf-8"))
    except ET.ParseError as e:
        raise HTTPException(400, f"Das XML ist nicht wohlgeformt: {e}") from e
    try:
        return sap.post_xml(req.xml)
    except requests.RequestException as e:
        log.exception("SAP request failed")
        raise HTTPException(502, f"SAP-Aufruf fehlgeschlagen: {e}") from e
