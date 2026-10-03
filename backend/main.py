"""FastAPI service for the material master search; also serves the UI5 frontend."""
import logging
import os
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from typing import Literal, Optional

import openai
import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai_search, bom, config, duplicates, sap, search, vectordb
from .data import load_materials

log = logging.getLogger("mdsa")


@asynccontextmanager
async def lifespan(_: FastAPI):
    search.index()   # load the input files and build the lexical index once at startup
    yield


app = FastAPI(title="Master Data Search Agent", lifespan=lifespan)


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


def openai_error(e: openai.OpenAIError) -> HTTPException:
    log.exception("OpenAI request failed")
    return HTTPException(502, f"OpenAI-Aufruf fehlgeschlagen: {e}")


@app.get("/api/config")
def get_config() -> dict:
    return {
        "materials": len(load_materials()),
        "indexed": vectordb.indexed_count(),
        "embedding_model": config.EMBEDDING_MODEL,
        "embedding_dimensions": config.EMBEDDING_DIMENSIONS,
        "model": config.MODEL,
        "llm_endpoint": config.OPENAI_BASE_URL or "https://api.openai.com/v1",
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "sap_endpoint": config.ENDPOINT_URL,
        "sap_configured": bool(os.getenv("SAP_USER") and os.getenv("SAP_PASSWORD")),
        "facets": search.facets(),
    }


# plain `def` endpoints run in FastAPI's thread pool, so OpenAI calls do not block other requests
@app.post("/api/search")
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


@app.get("/api/suggest")
def get_suggestions(q: str = Query(..., min_length=1)) -> list[dict]:
    return search.suggest(q)


@app.get("/api/materials/{matnr}")
def get_material(matnr: str) -> dict:
    m = load_materials().get(matnr)
    if m is None:
        raise HTTPException(404, f"Material {matnr} nicht gefunden")
    return m.detail()


@app.get("/api/materials/{matnr}/bom")
def get_bom(matnr: str, stlan: str = "", werks: str = "") -> dict:
    if matnr not in load_materials():
        raise HTTPException(404, f"Material {matnr} nicht gefunden")
    return bom.structure(matnr, stlan, werks)


@app.get("/api/materials/{matnr}/duplicates")
def get_duplicates(matnr: str) -> dict:
    try:
        return duplicates.candidates(matnr)
    except KeyError as e:
        raise HTTPException(404, f"Material {matnr} nicht gefunden") from e
    except openai.OpenAIError as e:
        raise openai_error(e) from e


@app.post("/api/materials/{matnr}/duplicates/assess")
def assess_duplicates(matnr: str, req: AssessRequest) -> dict:
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(500, "OPENAI_API_KEY ist nicht konfiguriert - keine KI-Bewertung möglich")
    try:
        return duplicates.assess(matnr, req.candidates)
    except KeyError as e:
        raise HTTPException(404, f"Material {matnr} nicht gefunden") from e
    except openai.OpenAIError as e:
        raise openai_error(e) from e


@app.get("/api/materials/{matnr}/xml")
def get_xml(matnr: str) -> dict:
    m = load_materials().get(matnr)
    if m is None:
        raise HTTPException(404, f"Material {matnr} nicht gefunden")
    return sap.message(m)


@app.post("/api/sap/post")
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


# UI5 app -- mounted last so the /api routes take precedence
app.mount("/", StaticFiles(directory=config.APP_DIR / "frontend", html=True), name="frontend")
