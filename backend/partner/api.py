"""REST API of the business partner search (/api/partners/...)."""
import logging
import os
import xml.etree.ElementTree as ET
from typing import Literal, Optional

import openai
import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..errors import openai_error
from ..material.sap import post_xml
from . import ai_search, deletion_check, duplicates, merge, sap, search
from .data import load_partners, table_labels

router = APIRouter(prefix="/api/partners")
log = logging.getLogger("mdsa")


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


class MergeGroup(BaseModel):
    partners: list[str]
    keep: Optional[str] = None


class MergeRequest(MergeGroup):
    keep: str
    choices: dict[str, str] = {}      # attribute group -> partner whose value is taken ("" = not taken over)
    take: list[str] = []              # keys of the list entries added to the kept partner
    actions: dict[str, list[str]] = {}   # other partner -> delete / central_block / customer_block / vendor_block


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
        result = duplicates.overview(max_tier, include_deleted, refresh)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    merged = merge.merged_in_current_extract()
    for c in result["clusters"]:
        c["merge"] = next((merged[p["partner"]] for p in c["partners"] if p["partner"] in merged), None)
    return result


# --- golden record (merge of a duplicate group) ---------------------------------
# declared before /{partner} so that "merge" is not taken for a partner number

def merge_errors(fn):
    try:
        return fn()
    except KeyError as e:
        raise HTTPException(404, f"Geschäftspartner {e.args[0]} nicht gefunden") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except openai.OpenAIError as e:
        raise openai_error(e) from e


@router.post("/merge/proposal")
def merge_proposal(req: MergeGroup) -> dict:
    """Partners of the group, suggested partner to keep, organisations and earlier merges."""
    return merge_errors(lambda: merge.proposal(req.partners))


@router.post("/merge/deletion-check/{partner}")
def merge_deletion_check(partner: str, refresh: bool = False) -> dict:
    """Deletion check of a group member; the result is kept for the validation before sending."""
    if not (os.getenv("SAP_USER") and os.getenv("SAP_PASSWORD")):
        raise HTTPException(500, "SAP_USER / SAP_PASSWORD sind auf dem Server nicht konfiguriert")
    p = load_partners().get(partner.lstrip("0") or partner)
    if p is None:
        raise HTTPException(404, f"Geschäftspartner {partner} nicht gefunden")
    return merge.deletion_status(p, refresh)


@router.post("/merge/advice")
def merge_advice(req: MergeGroup) -> dict:
    """Differing attributes and additional entries of the others, with the choice of the LLM."""
    if not req.keep:
        raise HTTPException(400, "Bitte den zu behaltenden Partner angeben")
    return merge_errors(lambda: merge.advice(req.partners, req.keep))


@router.post("/merge/preview")
def merge_preview(req: MergeRequest) -> dict:
    """Import message of the golden record (not sent)."""
    return merge_errors(lambda: merge.build(req.partners, req.keep, req.choices, req.take, req.actions))


@router.post("/merge/send")
def merge_send(req: MergeRequest) -> dict:
    """Validate (deletion flag only without open documents), send to the DXTO staging table and log."""
    if not (os.getenv("SAP_USER") and os.getenv("SAP_PASSWORD")):
        raise HTTPException(500, "SAP_USER / SAP_PASSWORD sind auf dem Server nicht konfiguriert")

    def run():
        merge.validate_actions(req.partners, req.keep, req.actions)
        return merge.build(req.partners, req.keep, req.choices, req.take, req.actions)
    plan = merge_errors(run)
    if not plan["xml"]:
        raise HTTPException(400, "Es gibt nichts zu senden")
    ET.fromstring(plan["xml"].encode("utf-8"))
    try:
        result = post_xml(plan["xml"])
    except requests.RequestException as e:
        log.exception("SAP request failed")
        result = {"ok": False, "status_code": None, "body": str(e)}
    entry = merge.write_log(plan, req.partners, req.actions, req.choices, req.take, result)
    return {**plan, "result": result, "log_id": entry}


@router.get("/merge/log")
def merge_log(partner: Optional[str] = None) -> list[dict]:
    return merge.history([partner.lstrip("0") or partner] if partner else None)


@router.get("/merge/log/{entry}")
def merge_log_entry(entry: int) -> dict:
    try:
        return merge.log_detail(entry)
    except KeyError as e:
        raise HTTPException(404, f"Protokolleintrag {entry} nicht gefunden") from e


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
