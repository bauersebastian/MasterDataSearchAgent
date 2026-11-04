"""FastAPI service for the master data search (material master and business partners); also serves the UI5
frontend. Each master data object has its own package (backend/material, backend/partner) with its API router."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config, vectordb
from .material import api as material_api
from .material import search as material_search
from .material.data import load_materials
from .partner import api as partner_api
from .partner import search as partner_search
from .partner.data import load_partners

log = logging.getLogger("mdsa")


def partners_available() -> bool:
    try:
        load_partners()
        return True
    except FileNotFoundError:
        return False


@asynccontextmanager
async def lifespan(_: FastAPI):
    # load the input files and build the lexical indexes once at startup
    material_search.index()
    if partners_available():
        partner_search.index()
    else:
        log.warning("Keine Geschäftspartnerdaten in %s - Geschäftspartnersuche deaktiviert", config.PARTNER_INPUT_DIR)
    yield


app = FastAPI(title="Master Data Search Agent", lifespan=lifespan)
app.include_router(material_api.router)
app.include_router(partner_api.router)


@app.get("/api/config")
def get_config() -> dict:
    partner = {"available": partners_available()}
    if partner["available"]:
        partner |= {"partners": len(load_partners()), "indexed": vectordb.indexed_count(config.PARTNER_COLLECTION),
                    "facets": partner_search.facets()}
    return {
        # material master (top level for compatibility)
        "materials": len(load_materials()),
        "indexed": vectordb.indexed_count(config.MATERIAL_COLLECTION),
        "facets": material_search.facets(),
        "partner": partner,
        "embedding_model": config.EMBEDDING_MODEL,
        "embedding_dimensions": config.EMBEDDING_DIMENSIONS,
        "model": config.MODEL,
        "llm_endpoint": config.OPENAI_BASE_URL or "https://api.openai.com/v1",
        "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
        "sap_endpoint": config.ENDPOINT_URL,
        "sap_configured": bool(os.getenv("SAP_USER") and os.getenv("SAP_PASSWORD")),
    }


# UI5 app -- mounted last so the /api routes take precedence
app.mount("/", StaticFiles(directory=config.APP_DIR / "frontend", html=True), name="frontend")
