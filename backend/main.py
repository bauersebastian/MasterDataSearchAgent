"""FastAPI service for the material master search; also serves the UI5 frontend."""
import logging
import os
from contextlib import asynccontextmanager
from typing import Literal, Optional

import openai
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, duplicates, search, vectordb
from .data import load_materials

log = logging.getLogger("mdsa")


@asynccontextmanager
async def lifespan(_: FastAPI):
    search.index()   # load the input files and build the lexical index once at startup
    yield


app = FastAPI(title="Master Data Search Agent", lifespan=lifespan)


class SearchRequest(BaseModel):
    query: str
    mode: Literal["hybrid", "semantic", "lexical"] = "hybrid"
    mtart: list[str] = []
    matkl: list[str] = []
    vendor: list[str] = []
    include_deleted: bool = False
    limit: int = 50


class AssessRequest(BaseModel):
    candidates: Optional[list[str]] = None


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
        "facets": search.facets(),
    }


# plain `def` endpoints run in FastAPI's thread pool, so OpenAI calls do not block other requests
@app.post("/api/search")
def run_search(req: SearchRequest) -> dict:
    if not req.query.strip():
        raise HTTPException(400, "Bitte einen Suchbegriff eingeben")
    if req.mode != "lexical" and not os.getenv("OPENAI_API_KEY"):
        if req.mode == "semantic":
            raise HTTPException(500, "OPENAI_API_KEY ist nicht konfiguriert - semantische Suche nicht möglich")
        req.mode = "lexical"
    try:
        return search.search(req.query, req.mode, req.model_dump(include={"mtart", "matkl", "vendor", "include_deleted"}),
                             req.limit)
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


# UI5 app -- mounted last so the /api routes take precedence
app.mount("/", StaticFiles(directory=config.APP_DIR / "frontend", html=True), name="frontend")
