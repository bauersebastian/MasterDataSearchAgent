"""Golden record: merge a group of duplicate business partners into one kept partner.

The kept partner is updated with the best data of the others (name, address, communication, bank, tax, ids, missing
customer / vendor role), the others get a deletion flag or blocks. The result is one DXTO import message (staging
table of the FIS add-on); it is reviewed, approved and posted to SAP in the add-on, the app only logs what it sent.

Rules of the add-on import:
- complete rows: every column of a table is sent, an empty value clears the field in SAP ("/" = leave unchanged);
- a new row whose key is a number gets a temporary key 9..0 of the field length (CONSNUMBER 900, BKVID 9000,
  ADDRNUMBER 9000000000), counting up; the add-on assigns the real number when posting;
- a new customer / vendor for an existing partner: KUNNR / LIFNR empty, the add-on numbers it internally;
- deletion: archiving flag BUT000-XDELE (G000) and deletion flag KNA1 / LFA1-LOEVM (GKA1 / GLA1). XDELE in the other
  tables marks a row for deletion in the add-on; it is used to remove a relationship that is moved to the kept
  partner (the XDELE column is only sent in a table that has such a row);
- the *FS columns (ADDRFS, PARTNERFS2, KTOFS) are for completely new business partners and stay empty."""
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from functools import lru_cache
from typing import Optional

from pydantic import BaseModel, Field

from .. import llm
from ..config import (ENDPOINT_URL, MERGE_BILLING_BLOCK, MERGE_CHECK_MAX_AGE, MERGE_DB, MERGE_DELIVERY_BLOCK,
                      MERGE_ORDER_BLOCK, MERGE_SALES_BLOCK, MODEL, PARTNER_INPUT_DIR, PARTNER_KEY_FIELD,
                      PARTNER_VARIANT)
from ..material.sap import build_message
from ..tables import strip_zeros, table_file
from . import deletion_check
from .data import TYPES, Partner, country_name, load_partners, today

log = logging.getLogger("mdsa")

# --- what can be merged ----------------------------------------------------------

NAME_FIELDS = {
    "1": ["TITLE_ACA1", "NAME_FIRST", "NAMEMIDDLE", "PREFIX1", "NAME_LAST", "NAME_LAST2"],
    "2": ["NAME_ORG1", "NAME_ORG2", "NAME_ORG3", "NAME_ORG4"],
    "3": ["NAME_GRP1", "NAME_GRP2"],
}
# fields of the standard address that are taken over together (never street from one and city from another partner)
ADDRESS_FIELDS = [
    "NAME_CO", "CITY1", "CITY2", "CITY_CODE", "CITYP_CODE", "CITY_CODE2", "CITYH_CODE", "HOME_CITY", "REGIOGROUP",
    "POST_CODE1", "POST_CODE2", "POST_CODE3", "PO_BOX", "PO_BOX_NUM", "PO_BOX_LOC", "PO_BOX_REG", "PO_BOX_CTY",
    "PO_BOX_LOBBY", "DELI_SERV_TYPE", "DELI_SERV_NUMBER", "STREET", "STREETCODE", "HOUSE_NUM1", "HOUSE_NUM2",
    "STR_SUPPL1", "STR_SUPPL2", "STR_SUPPL3", "LOCATION", "BUILDING", "FLOOR", "ROOMNUMBER", "COUNTRY", "REGION",
    "TIME_ZONE", "TAXJURCODE", "TRANSPZONE", "LANGU", "CHCKSTATUS",
]
CUSTOMER_ROLES = ("FLCU00", "FLCU01")
VENDOR_ROLES = ("FLVN00", "FLVN01")
# cleared when the customer / vendor of another partner is taken over (it is the active one from now on)
CUSTOMER_FLAGS = ("LOEVM", "SPERR", "AUFSD", "LIFSD", "FAKSD", "CASSD", "NODEL")
VENDOR_FLAGS = ("LOEVM", "SPERR", "SPERM", "SPERQ", "SPERZ", "NODEL")

# communication tables: kind, label, value columns
COMM_TABLES = {"GAD2": ("phone", "Telefon"), "GAD3": ("fax", "Fax"), "GAD6": ("email", "E-Mail"),
               "GADU": ("url", "Webseite")}
ITEM_TABLES = {**COMM_TABLES, "G0BK": ("bank", "Bankverbindung"), "GTAX": ("tax", "Steuernummer"),
               "G0ID": ("id", "Identifikationsnummer")}

RELATION_TABLES = ("GRAC", "GRA2", "GRA3", "GRA6")   # address and communication of a relationship

ACTIONS = {"delete": "Löschvormerkung", "central_block": "Zentrale Sperre", "customer_block": "Kundensperre",
           "vendor_block": "Lieferantensperre"}


def default_address(p: Partner) -> dict:
    rows = p.table("GADC")
    return next((r for r in rows if r.get("XDFADR") == "X"), rows[0] if rows else {})


def address_text(row: dict) -> str:
    street = " ".join(x for x in (row.get("STREET"), row.get("HOUSE_NUM1"), row.get("HOUSE_NUM2")) if x)
    city = " ".join(x for x in (row.get("POST_CODE1"), row.get("CITY1")) if x)
    po_box = f"Postfach {row['PO_BOX']} {row.get('POST_CODE2', '')}".strip() if row.get("PO_BOX") else ""
    parts = [row.get("NAME_CO") and f"c/o {row['NAME_CO']}", street, row.get("STR_SUPPL1"), po_box, city,
             row.get("CITY2"), row.get("REGION") and f"Region {row['REGION']}", country_name(row.get("COUNTRY", ""))]
    return ", ".join(x for x in parts if x)


def sales_areas(p: Partner) -> list[str]:
    return sorted({"/".join((r.get("VKORG", ""), r.get("VTWEG", ""), r.get("SPART", ""))) for r in p.table("GKVP")})


def purchasing_orgs(p: Partner) -> list[str]:
    return sorted({r.get("EKORG", "") for r in p.table("GLT3") if r.get("EKORG")})


def item_key(table: str, r: dict) -> str:
    """Comparable value of a list entry (phone digits, lower case e-mail, IBAN, ...)."""
    digits = lambda v: re.sub(r"\D", "", v or "")   # noqa: E731
    if table == "GAD2":
        return digits(r.get("TEL_NUMBER"))[-9:] + "-" + digits(r.get("TEL_EXTENS")) if r.get("TEL_NUMBER") else ""
    if table == "GAD3":
        return digits(r.get("FAX_NUMBER"))[-9:] + "-" + digits(r.get("FAX_EXTENS")) if r.get("FAX_NUMBER") else ""
    if table == "GAD6":
        return (r.get("SMTP_ADDR") or "").strip().lower()
    if table == "GADU":
        return re.sub(r"^(https?://)?(www\.)?", "", (r.get("URI_ADDR") or "").strip().lower()).rstrip("/")
    if table == "G0BK":
        if r.get("IBAN"):
            return r["IBAN"].replace(" ", "").upper()
        return "/".join((r.get("BANKS", ""), r.get("BANKL", ""), r.get("BANKN", ""))) if r.get("BANKN") else ""
    if table == "GTAX":
        return f"{r.get('TAXTYPE', '')}:{re.sub(r'[^0-9A-Z]', '', (r.get('TAXNUM') or '').upper())}" \
            if r.get("TAXNUM") else ""
    if table == "G0ID":
        return f"{r.get('TYPE', '')}:{(r.get('IDNUMBER') or '').strip().upper()}" if r.get("IDNUMBER") else ""
    return ""


def item_text(table: str, r: dict) -> str:
    if table == "GAD2":
        return (r.get("TELNR_LONG") or r.get("TEL_NUMBER", "")) + (" (mobil)" if r.get("R3_USER") == "3" else "")
    if table == "GAD3":
        return r.get("FAXNR_LONG") or r.get("FAX_NUMBER", "")
    if table == "GAD6":
        return r.get("SMTP_ADDR", "")
    if table == "GADU":
        return r.get("URI_ADDR", "")
    if table == "G0BK":
        bank = r.get("IBAN") or f"{r.get('BANKS', '')} {r.get('BANKL', '')} {r.get('BANKN', '')}"
        return bank + (f" ({r['KOINH']})" if r.get("KOINH") else "")
    if table == "GTAX":
        return f"{r.get('TAXTYPE', '')}: {r.get('TAXNUM', '')}"
    if table == "G0ID":
        return f"{r.get('TYPE', '')}: {r.get('IDNUMBER', '')}" + (f" ({r['INSTITUTE']})" if r.get("INSTITUTE") else "")
    return ""


# --- keeper suggestion -------------------------------------------------------------

def keep_score(p: Partner) -> tuple[float, list[str]]:
    """Which partner should survive: not deleted / blocked, customer and vendor, most complete and most used."""
    score, reasons = 0.0, []
    if p.deleted:
        score -= 100
        reasons.append("hat eine Löschvormerkung")
    if p.blocks:
        score -= 30
        reasons.append("ist gesperrt")
    if p.kunnr and p.lifnr:
        score += 30
        reasons.append("ist Kunde und Lieferant")
    elif p.kunnr or p.lifnr:
        score += 15
        reasons.append("ist Kunde" if p.kunnr else "ist Lieferant")
    if p.materials:
        score += min(20, 5 + len(p.materials) / 10)
        reasons.append(f"{len(p.materials)} Einkaufsinfosätze")
    if p.contacts:
        score += min(10, 2 * len(p.contacts))
        reasons.append(f"{len(p.contacts)} Ansprechpartner")
    areas = len(sales_areas(p)) + len(purchasing_orgs(p))
    if areas:
        score += min(10, 2 * areas)
        reasons.append(f"{areas} Vertriebsbereiche / Einkaufsorganisationen")
    filled = sum(1 for v in p.g000.values() if v) + sum(len(r) for r in p.rows.values())
    score += min(10, filled / 10)
    return score, reasons


def proposal(ids: list[str]) -> dict:
    """Partners of the group with the suggested partner to keep and the hints on roles and organisations."""
    partners = group(ids)
    scored = {p.id: keep_score(p) for p in partners}
    # tie: the older (lower) partner number
    keep = max(partners, key=lambda p: (scored[p.id][0], -len(p.id), [-ord(c) for c in p.id])).id
    return {
        "keep": keep,
        "partners": [{**p.summary(), "type_text": TYPES.get(p.type, p.type), "score": round(scored[p.id][0]),
                      "reasons": scored[p.id][1], "sales_areas": sales_areas(p), "purchasing_orgs": purchasing_orgs(p),
                      "has_customer": bool(p.customer), "has_vendor": bool(p.vendor)} for p in partners],
        "log": history([p.id for p in partners]),
    }


def group(ids: list[str]) -> list[Partner]:
    partners = load_partners()
    ids = list(dict.fromkeys(i.lstrip("0") or i for i in ids))
    missing = [i for i in ids if i not in partners]
    if missing:
        raise KeyError(", ".join(missing))
    if len(ids) < 2:
        raise ValueError("Für eine Zusammenführung werden mindestens zwei Geschäftspartner benötigt")
    return [partners[i] for i in ids]


# --- differences: groups (choose a partner) and list entries (take or not) ------

def field_groups(keep: Partner, others: list[Partner]) -> list[dict]:
    """Attribute groups whose values differ between the partners; options = partners with a value."""
    everyone = [keep] + others
    defs = [("name", "Name", "G000", NAME_FIELDS.get(keep.type, NAME_FIELDS["2"])),
            ("search_terms", "Suchbegriffe", "G000", ["BU_SORT1", "BU_SORT2"]),
            ("title", "Anrede", "G000", ["TITLE"]),
            ("language", "Sprache", "G000", ["BU_LANGU"]),
            ("bpext", "Externe Partnernummer", "G000", ["BPEXT"])]
    if keep.type == "1":
        defs.append(("birthdate", "Geburtsdatum", "G000", ["BIRTHDT"]))
    else:
        defs += [("legal_form", "Rechtsform", "G000", ["LEGAL_ENTY"]), ("founded", "Gründungsdatum", "G000", ["FOUND_DAT"])]
    defs.append(("address", "Standardadresse", "GADC", ADDRESS_FIELDS))

    result = []
    for key, label, table, fields in defs:
        options = []
        for p in everyone:
            row = p.g000 if table == "G000" else default_address(p)
            values = {f: row.get(f, "") for f in fields}
            text = address_text(row) if table == "GADC" else " ".join(v for v in values.values() if v)
            options.append({"partner": p.id, "text": text, "values": values})
        texts = {re.sub(r"\W", "", o["text"].lower()) for o in options if o["text"]}
        if len(texts) <= 1 and options[0]["text"]:
            continue   # all the same (or only the kept partner has a value)
        options = [o for o in options if o["text"] or o["partner"] == keep.id]
        if len(options) > 1:
            result.append({"key": key, "label": label, "table": table, "fields": fields, "options": options,
                           "choice": keep.id if options[0]["text"] else options[1]["partner"]})

    # customer / vendor role of another partner, if the kept partner has none
    for key, label, table, attr in (("customer", "Kundenrolle übernehmen", "GKA1", "customer"),
                                    ("vendor", "Lieferantenrolle übernehmen", "GLA1", "vendor")):
        if getattr(keep, attr):
            continue
        sources = [p for p in others if getattr(p, attr)]
        if not sources:
            continue
        options = [{"partner": "", "text": "nicht übernehmen", "values": {}}]
        for p in sources:
            number = p.kunnr if attr == "customer" else p.lifnr
            group_field = "KTOKD" if attr == "customer" else "KTOKK"
            text = (f"{'Kunde' if attr == 'customer' else 'Lieferant'} {number} von {p.id}, Kontengruppe "
                    f"{getattr(p, attr).get(group_field, '')}" + (" (Löschvormerkung)" if p.deleted else ""))
            options.append({"partner": p.id, "text": text, "values": {}})
        best = max(sources, key=lambda p: (not p.deleted, not p.blocks, len(p.materials), len(p.table("GKVP"))))
        result.append({"key": key, "label": label, "table": table, "fields": [], "options": options,
                       "choice": best.id})
    return result


def list_items(keep: Partner, others: list[Partner]) -> list[dict]:
    """Communication, bank, tax and id entries of the other partners that the kept partner does not have."""
    items, seen = [], set()
    for table, (kind, label) in ITEM_TABLES.items():
        own = {item_key(table, r) for r in keep.table(table)}
        own_tax_types = {r.get("TAXTYPE") for r in keep.table("GTAX")}
        for p in others:
            for r in p.table(table):
                k = item_key(table, r)
                if not k or k in own or (table, k) in seen:
                    continue
                seen.add((table, k))
                replaces = ""
                if table == "GTAX" and r.get("TAXTYPE") in own_tax_types:
                    old = next(x for x in keep.table("GTAX") if x.get("TAXTYPE") == r.get("TAXTYPE"))
                    replaces = item_text("GTAX", old)
                items.append({"key": f"{table}:{k}", "table": table, "kind": kind, "label": label,
                              "text": item_text(table, r), "partner": p.id, "replaces": replaces,
                              "take": not replaces and not p.deleted, "reason": ""})
    return items + relation_items(keep, others)


def relation_key(r: dict) -> tuple:
    return r.get("PARTNER2"), r.get("RELTYP"), r.get("DIRECTION"), r.get("LFDNR")


def relation_items(keep: Partner, others: list[Partner]) -> list[dict]:
    """Relationships (contact persons, ...) of the other partners, moved to the kept partner as new relationships.
    Not offered: relationships within the group and those the kept partner already has (same partner and type)."""
    group_numbers = {keep.partner} | {p.partner for p in others}
    own = {(r.get("PARTNER2"), r.get("RELTYP")) for r in keep.table("G050")}
    partners = load_partners()
    items = []
    for p in others:
        details = {(x["partner"], x["reltyp"], x["direction"]): x for x in p.relations}
        for r in p.table("G050"):
            if r.get("PARTNER2") in group_numbers or (r.get("PARTNER2"), r.get("RELTYP")) in own:
                continue
            other = partners.get(strip_zeros(r.get("PARTNER2", "")))
            info = details.get((strip_zeros(r.get("PARTNER2", "")), r.get("RELTYP"), r.get("DIRECTION")), {})
            active = info.get("active", True)
            label = info.get("label") or ""
            label = r.get("RELTYP", "") if not label or label.startswith("Beziehung ") else label
            text = f"{label}: {other.display_name if other else ''} " \
                   f"({strip_zeros(r.get('PARTNER2', ''))})"
            extra = [x for x in (info.get("function"), info.get("department")) if x]
            if extra:
                text += ", " + ", ".join(extra)
            if not active:
                text += " – nicht mehr gültig"
            items.append({"key": "G050:" + ":".join(relation_key(r)) + f":{p.id}", "table": "G050",
                          "kind": "relation", "label": "Beziehung", "text": text, "partner": p.id, "replaces": "",
                          "take": active and not (other and other.deleted), "reason": ""})
    return items


# --- LLM advice ---------------------------------------------------------------------

class GroupChoice(BaseModel):
    key: str = Field(description="Key of the attribute group")
    partner: str = Field(description="Partner number whose value is best; empty string = do not take over "
                                     "(only allowed for the customer / vendor role groups)")
    reason: str = Field(description="Short reason in German")


class ItemChoice(BaseModel):
    key: str = Field(description="Key of the list entry")
    take: bool = Field(description="Add the entry to the kept partner")
    reason: str = Field(description="Short reason in German")


class MergeAdvice(BaseModel):
    summary: str = Field(description="One to three sentences in German on the golden record")
    groups: list[GroupChoice]
    items: list[ItemChoice]


ADVICE_PROMPT = """You are an SAP business partner master data steward building a golden record: several business
partners are the same company or person and are merged into the kept partner. The other partners are deleted or
blocked afterwards, so data they have and the kept partner lacks is lost unless it is taken over.

For each attribute group choose the partner whose value is best: complete, correctly spelled and formatted, current
(not of a partner with a deletion flag or block unless it is clearly better), consistent with the other data (postcode
matches city and country, legal form matches the name). Prefer the kept partner's value if it is equally good. For
the customer / vendor role groups: the kept partner has no such role -- take it over from the most active, not
deleted partner (an empty partner only if none of them is a real customer / vendor of the company).
For each list entry decide whether to add it to the kept partner: add valid, plausible communication, bank, tax and
identification data; do not add obvious test or placeholder values, entries that duplicate an existing one in another
spelling, or a tax number that would replace a valid one of the kept partner without a reason.
Relationships (kind "Beziehung", mostly contact persons) of the other partners are moved to the kept partner: take
valid ones; not those no longer valid, of test / placeholder partners, or a contact person the kept partner already
has under another partner number (same name).
Reply in German."""


def advice(ids: list[str], keep_id: str) -> dict:
    """Groups and list entries with the LLM's choice (without OPENAI_API_KEY: the default heuristic)."""
    partners = group(ids)
    keep, others = split(partners, keep_id)
    groups, items = field_groups(keep, others), list_items(keep, others)
    result = {"groups": groups, "items": items, "summary": "", "model": None}
    if not (groups or items) or not llm_available():
        return result
    request = {
        "kept_partner": keep.id,
        "partners": [p.llm_view(materials=5) for p in partners],
        "attribute_groups": [{"key": g["key"], "label": g["label"],
                              "options": [{"partner": o["partner"], "value": o["text"]} for o in g["options"]]}
                             for g in groups],
        "list_entries": [{"key": i["key"], "kind": i["label"], "value": i["text"], "from_partner": i["partner"],
                          **({"replaces": i["replaces"]} if i["replaces"] else {})} for i in items],
    }
    answer = llm.parse(ADVICE_PROMPT, json.dumps(request, ensure_ascii=False), MergeAdvice)
    by_key = {g["key"]: g for g in groups}
    for c in answer.groups:
        g = by_key.get(c.key)
        partner = c.partner.lstrip("0") or c.partner if c.partner else ""
        if g and any(o["partner"] == partner for o in g["options"]):
            g["choice"], g["reason"] = partner, c.reason
    by_key = {i["key"]: i for i in items}
    for c in answer.items:
        if c.key in by_key:
            by_key[c.key]["take"], by_key[c.key]["reason"] = c.take, c.reason
    result.update(summary=answer.summary, model=MODEL)
    return result


def llm_available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def split(partners: list[Partner], keep_id: str) -> tuple[Partner, list[Partner]]:
    keep_id = keep_id.lstrip("0") or keep_id
    keep = next((p for p in partners if p.id == keep_id), None)
    if keep is None:
        raise ValueError(f"Der zu behaltende Partner {keep_id} gehört nicht zur Gruppe")
    return keep, [p for p in partners if p.id != keep_id]


# --- import message ----------------------------------------------------------------------

@lru_cache(maxsize=None)
def columns(table: str) -> list[str]:
    """All columns of a GPA table in extract order (complete rows are sent)."""
    with open(table_file(PARTNER_INPUT_DIR, table), encoding="utf-8") as fh:
        return [c.strip() for c in fh.readline().strip().split(";") if c.strip()]


def message_fields(table: str, rows: list[dict]) -> list[str]:
    """Key field + all columns; XDELE (row deletion marker) only in tables with a row to delete -- in G000 it is the
    archiving flag and always sent."""
    marked = table == "G000" or any(r.get("XDELE") == "X" for r in rows)
    skip = {"PARTNER", PARTNER_KEY_FIELD} | (set() if marked else {"XDELE"})
    return [PARTNER_KEY_FIELD] + [c for c in columns(table) if c not in skip]


def new_key(length: int, used: set[str]) -> str:
    """Temporary key of a new row: 9 followed by zeros (field length), the next free one."""
    n = 9 * 10 ** (length - 1)
    while str(n) in used or str(n).zfill(length) in used:
        n += 1
    used.add(str(n))
    return str(n)


class Plan:
    """Rows of the message plus a readable list of the changes."""

    def __init__(self):
        self.rows: dict[str, list[dict]] = {}
        self.changes: list[dict] = []

    def add(self, partner: Partner, table: str, row: dict, kind: str, text: str, old: Optional[dict] = None):
        row = {c: row.get(c, "") for c in columns(table)}
        row["PARTNER"] = partner.partner
        self.rows.setdefault(table, []).append(row)
        fields = []
        for f, v in row.items():
            before = (old or {}).get(f, "")
            if f != "PARTNER":
                if (old is not None and v != before) or (old is None and v):
                    fields.append({"field": f, "old": before if old is not None else "", "new": v})
        self.changes.append({"partner": partner.id, "table": table, "kind": kind, "text": text, "fields": fields})


def build(ids: list[str], keep_id: str, choices: dict[str, str], take: list[str],
          actions: dict[str, list[str]]) -> dict:
    """The import message for the golden record: kept partner updated, the others flagged / blocked."""
    partners = group(ids)
    keep, others = split(partners, keep_id)
    by_id = {p.id: p for p in partners}
    plan, warnings = Plan(), []
    groups = {g["key"]: g for g in field_groups(keep, others)}

    # --- kept partner: general data (G000)
    g000, changed = dict(keep.g000), []
    for key, g in groups.items():
        source = choices.get(key, g["choice"])
        if g["table"] != "G000" or not source or source == keep.id:
            continue
        g000.update({f: by_id[source].g000.get(f, "") for f in g["fields"]})
        changed.append(f"{g['label']} von {source}")
    if changed:
        plan.add(keep, "G000", g000, "Ändern", "Allgemeine Daten: " + ", ".join(changed), keep.g000)

    # --- kept partner: standard address (GADC); the existing address number is kept for the change
    own_address = default_address(keep)
    address_numbers = {r.get("ADDRNUMBER") for r in keep.table("GADC")}
    address_number = own_address.get("ADDRNUMBER", "")
    g = groups.get("address")
    source = choices.get("address", g["choice"]) if g else ""
    if g and source and source != keep.id:
        if own_address:
            row = {**own_address, **{f: default_address(by_id[source]).get(f, "") for f in ADDRESS_FIELDS}}
            plan.add(keep, "GADC", row, "Ändern", f"Standardadresse {strip_zeros(address_number)} mit der Adresse "
                                                  f"von {source}", own_address)
        else:   # the kept partner has no address: new standard address with its usage
            address_number = new_key(10, address_numbers)
            row = {**default_address(by_id[source]), "ADDRNUMBER": address_number, "ADDRFS": "", "XDFADR": "X",
                   "ADEXT": "", "MOVE_ADDR": "", "ADDR_MOVE_DATE": ""}
            plan.add(keep, "GADC", row, "Neu", f"Standardadresse (neu, vorläufige Nr. {address_number}) von {source}")
            plan.add(keep, "G021", {"ADDRNUMBER": address_number, "ADR_KIND": "XXDEFAULT", "CONSNUMBER": "001",
                                    "VALID_FROM": "", "VALID_TO": ""}, "Neu", "Adressverwendung Standard")
    if not address_number and any(t in COMM_TABLES for t in {i.split(":", 1)[0] for i in take}):
        warnings.append("Der behaltene Partner hat keine Adresse - Kommunikationsdaten können nicht übernommen werden.")

    # --- kept partner: list entries
    items = {i["key"]: i for i in list_items(keep, others)}
    used: dict[str, set[str]] = {}
    defaults: set[str] = set()
    for key in take:
        item = items.get(key)
        if not item:
            continue
        table, source = item["table"], by_id[item["partner"]]
        if table == "G050":
            move_relation(plan, keep, source, key, address_number, used)
            continue
        original = next(r for r in source.table(table) if f"{table}:{item_key(table, r)}" == key)
        row = dict(original)
        if table in COMM_TABLES:
            if not address_number:
                continue
            numbers = used.setdefault(table, {r.get("CONSNUMBER", "") for r in keep.table(table)
                                              if r.get("ADDRNUMBER") == address_number})
            # the kept partner's default number / e-mail stays the default; the first new one only if it has none
            has_default = table in defaults or any(r.get("FLGDEFAULT") == "X" for r in keep.table(table)
                                                   if r.get("ADDRNUMBER") == address_number)
            defaults.add(table)
            row.update(ADDRNUMBER=address_number, ADDRFS="", CONSNUMBER=new_key(3, numbers),
                       FLGDEFAULT="" if has_default else "X", DFT_RECEIV="")
            plan.add(keep, table, row, "Neu", f"{item['label']} {item['text']} von {source.id}")
        elif table == "G0BK":
            numbers = used.setdefault(table, {r.get("BKVID", "") for r in keep.table("G0BK")})
            row.update(BKVID=new_key(4, numbers), MOVE_BKVID="", BK_MOVE_DATE="")
            plan.add(keep, table, row, "Neu", f"Bankverbindung {item['text']} von {source.id}")
        elif table == "GTAX":
            old = next((r for r in keep.table("GTAX") if r.get("TAXTYPE") == row.get("TAXTYPE")), None)
            plan.add(keep, table, row, "Ändern" if old else "Neu",
                     f"Steuernummer {item['text']} von {source.id}" + (f" (ersetzt {item['replaces']})" if old else ""),
                     old)
        else:
            plan.add(keep, table, row, "Neu", f"{item['label']} {item['text']} von {source.id}")

    # --- kept partner: customer / vendor role of another partner (new number assigned by the add-on)
    for key, table, roles, number_field, flags, label in (
            ("customer", "GKA1", CUSTOMER_ROLES, "KUNNR", CUSTOMER_FLAGS, "Kunde"),
            ("vendor", "GLA1", VENDOR_ROLES, "LIFNR", VENDOR_FLAGS, "Lieferant")):
        g = groups.get(key)
        source = choices.get(key, g["choice"]) if g else ""
        if not g or not source:
            continue
        p = by_id[source]
        own_roles = set(keep.role_codes)
        for r in p.table("G100"):
            if r.get("RLTYP") in roles and r["RLTYP"] not in own_roles:
                plan.add(keep, "G100", {**r, "VALID_FROM": today(), "VALID_TO": "99991231"}, "Neu",
                         f"Rolle {r['RLTYP']}")
        row = {**p.table(table)[0], number_field: "", **{f: "" for f in flags}}
        old_number = p.kunnr if key == "customer" else p.lifnr
        plan.add(keep, table, row, "Neu", f"{label} (neue Nummer vom Add-on) mit den Daten von {label} {old_number}")
        if key == "customer" and sales_areas(p):
            warnings.append(f"Die Vertriebsbereiche von Kunde {old_number} ({', '.join(sales_areas(p))}) sind nicht "
                            "im Extrakt - bitte im Add-on für den neuen Kunden anlegen.")
        if key == "vendor" and purchasing_orgs(p):
            warnings.append(f"Die Einkaufsorganisationen von Lieferant {old_number} ({', '.join(purchasing_orgs(p))}) "
                            "sind nicht im Extrakt - bitte im Add-on für den neuen Lieferanten anlegen.")

    # organisations of the others that the kept partner does not have
    own_areas, own_orgs = set(sales_areas(keep)), set(purchasing_orgs(keep))
    missing_areas = sorted({a for p in others for a in sales_areas(p)} - own_areas)
    missing_orgs = sorted({o for p in others for o in purchasing_orgs(p)} - own_orgs)
    if keep.customer and missing_areas:
        warnings.append(f"Vertriebsbereiche der anderen Partner fehlen beim behaltenen Partner: "
                        f"{', '.join(missing_areas)} - ggf. im Add-on anlegen.")
    if keep.vendor and missing_orgs:
        warnings.append(f"Einkaufsorganisationen der anderen Partner fehlen beim behaltenen Partner: "
                        f"{', '.join(missing_orgs)} - ggf. im Add-on anlegen.")
    left = [i for k, i in items.items() if i["table"] == "G050" and k not in take]
    if left:
        warnings.append(f"{len(left)} Beziehung(en) der anderen Partner werden nicht übernommen: "
                        + "; ".join(f"{i['text']} (bei {i['partner']})" for i in left[:5])
                        + (" …" if len(left) > 5 else ""))
    if keep.deleted or keep.blocks:
        warnings.append(f"Der behaltene Partner {keep.id} ist gelöscht oder gesperrt "
                        f"({', '.join(keep.blocks + (['Löschvormerkung'] if keep.deleted else []))}).")

    # --- the other partners: deletion flag and / or blocks
    for p in others:
        chosen = [a for a in actions.get(p.id, []) if a in ACTIONS]
        if not chosen:
            warnings.append(f"Für {p.id} ist weder Löschvormerkung noch Sperre gewählt.")
            continue
        g000 = dict(p.g000)
        if "delete" in chosen:
            g000["XDELE"] = "X"
        if "central_block" in chosen:
            g000["XBLCK"] = "X"
        if g000 != p.g000:
            plan.add(p, "G000", g000, "Ändern", ", ".join(ACTIONS[a] for a in chosen if a in ("delete", "central_block"))
                     + f" (Dublette von {keep.id})", p.g000)
        if p.customer:
            row = dict(p.customer)
            if "delete" in chosen:
                row["LOEVM"] = "X"
            if "customer_block" in chosen:
                row["SPERR"] = "X"
                for f, code in (("AUFSD", MERGE_ORDER_BLOCK), ("LIFSD", MERGE_DELIVERY_BLOCK),
                                ("FAKSD", MERGE_BILLING_BLOCK), ("CASSD", MERGE_SALES_BLOCK)):
                    if code:
                        row[f] = code
            if row != p.customer:
                plan.add(p, "GKA1", row, "Ändern", f"Kunde {p.kunnr}: " + ", ".join(
                    ACTIONS[a] for a in chosen if a in ("delete", "customer_block")), p.customer)
        if p.vendor:
            row = dict(p.vendor)
            if "delete" in chosen:
                row["LOEVM"] = "X"
            if "vendor_block" in chosen:
                row.update(SPERR="X", SPERM="X")
            if row != p.vendor:
                plan.add(p, "GLA1", row, "Ändern", f"Lieferant {p.lifnr}: " + ", ".join(
                    ACTIONS[a] for a in chosen if a in ("delete", "vendor_block")), p.vendor)

    order = [t for t in columns_order() if t in plan.rows]
    xml = build_message({t: (message_fields(t, plan.rows[t]), [{**r, PARTNER_KEY_FIELD: r["PARTNER"]}
                                                              for r in plan.rows[t]])
                         for t in order}, PARTNER_VARIANT) if plan.rows else ""
    return {"keep": keep.id, "others": [p.id for p in others], "changes": plan.changes, "warnings": warnings,
            "tables": [{"table": t, "rows": len(plan.rows[t])} for t in order], "xml": xml,
            "file": f"golden_record_{keep.id}.xml", "endpoint": ENDPOINT_URL, "variant": PARTNER_VARIANT}


def move_relation(plan: "Plan", keep: Partner, source: Partner, key: str, address_number: str,
                  used: dict[str, set[str]]) -> None:
    """New relationship of the kept partner with the data of the source's relationship (G050 + address and
    communication GRAC / GRA2 / GRA3 / GRA6, linked by PARTNER2 + RELTYP + DIRECTION + LFDNR); the old one is marked
    for deletion (XDELE). LFDNR of the new one: temporary key 900, ... (the add-on numbers it).
    The address of a relationship is the organisation's: if it is the source's own address, the kept partner's
    standard address is used, otherwise (the source is the person) it stays."""
    old_key = tuple(key.split(":")[1:5])
    row = next(r for r in source.table("G050") if relation_key(r) == old_key)
    numbers = used.setdefault(f"LFDNR:{old_key[0]}:{old_key[1]}:{old_key[2]}",
                              {r.get("LFDNR", "") for r in keep.table("G050") if relation_key(r)[:3] == old_key[:3]})
    lfdnr = new_key(3, numbers)
    own_addresses = {r.get("ADDRNUMBER") for r in source.table("GADC")}
    name = f"{strip_zeros(old_key[0])} ({old_key[1]})"
    plan.add(keep, "G050", {**row, "LFDNR": lfdnr, "PARTNERFS2": "", "XDELE": ""}, "Neu",
             f"Beziehung {name} von {source.id} übernommen (vorläufige Nr. {lfdnr})")
    for table in RELATION_TABLES:
        for r in source.table(table):
            if relation_key(r) != old_key:
                continue
            new = {**r, "LFDNR": lfdnr, "PARTNERFS2": "", "ADDRFS": "", "XDELE": ""}
            if r.get("ADDRNUMBER") in own_addresses:
                new["ADDRNUMBER"] = address_number
            plan.add(keep, table, new, "Neu", f"Beziehung {name}: {table}")
    plan.add(source, "G050", {**row, "XDELE": "X"}, "Löschen",
             f"Beziehung {name} entfernt (zu {keep.id} verschoben)", row)


def columns_order() -> list[str]:
    from .data import tables_in_extract
    return tables_in_extract()


# --- deletion check of the group (cached for the validation before sending) ---------

_checks: dict[str, dict] = {}
_checks_lock = threading.Lock()


def deletion_status(p: Partner, refresh: bool = False) -> dict:
    with _checks_lock:
        cached = _checks.get(p.id)
    age = (datetime.now() - datetime.fromisoformat(cached["checked_at"])).total_seconds() if cached else None
    if refresh or cached is None or age > MERGE_CHECK_MAX_AGE:
        cached = deletion_check.check(p)
        with _checks_lock:
            _checks[p.id] = cached
    return cached


def validate_actions(ids: list[str], keep_id: str, actions: dict[str, list[str]]) -> None:
    """A deletion flag is only allowed when the deletion check found no open documents."""
    partners = group(ids)
    keep, others = split(partners, keep_id)
    for p in others:
        chosen = actions.get(p.id, [])
        if "delete" in chosen:
            status = deletion_status(p)
            if status["verdict"] != "ok":
                raise ValueError(f"{p.id} kann nicht zum Löschen vorgemerkt werden: {status['text']}")
        if "customer_block" in chosen and not p.customer:
            raise ValueError(f"{p.id} ist kein Kunde - Kundensperre nicht möglich")
        if "vendor_block" in chosen and not p.vendor:
            raise ValueError(f"{p.id} ist kein Lieferant - Lieferantensperre nicht möglich")


# --- log (SQLite) ----------------------------------------------------------------------

def extract_id() -> str:
    """Timestamp of the extract (GPA_<date>_<time>_...): merged partners disappear with the next extract."""
    m = re.search(r"_(\d{8}_\d{6})_", table_file(PARTNER_INPUT_DIR, "G000").name)
    return m.group(1) if m else ""


def db() -> sqlite3.Connection:
    MERGE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(MERGE_DB)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS merges (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, extract TEXT, keep TEXT NOT NULL,
        partners TEXT NOT NULL, actions TEXT, choices TEXT, items TEXT, changes TEXT, warnings TEXT, xml TEXT,
        status TEXT NOT NULL, http_status INTEGER, response TEXT)""")
    return con


def write_log(plan: dict, ids: list[str], actions: dict, choices: dict, take: list[str], result: dict) -> int:
    with db() as con:
        cur = con.execute(
            "INSERT INTO merges (created_at, extract, keep, partners, actions, choices, items, changes, warnings, xml, "
            "status, http_status, response) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), extract_id(), plan["keep"],
             json.dumps([plan["keep"]] + plan["others"]), json.dumps(actions), json.dumps(choices), json.dumps(take),
             json.dumps(plan["changes"], ensure_ascii=False), json.dumps(plan["warnings"], ensure_ascii=False),
             plan["xml"], "sent" if result.get("ok") else "failed", result.get("status_code"),
             (result.get("body") or "")[:20000]))
        return cur.lastrowid


def history(ids: Optional[list[str]] = None, limit: int = 200) -> list[dict]:
    """Logged merges, newest first; with `ids` only those touching one of the partners."""
    if not MERGE_DB.exists():
        return []
    with db() as con:
        rows = con.execute("SELECT id, created_at, extract, keep, partners, actions, warnings, status, http_status "
                           "FROM merges ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    result = []
    current = extract_id()
    for r in rows:
        partners = json.loads(r["partners"])
        if ids and not set(partners) & set(ids):
            continue
        result.append({**dict(r), "partners": partners, "actions": json.loads(r["actions"] or "{}"),
                       "warnings": json.loads(r["warnings"] or "[]"), "current_extract": r["extract"] == current})
    return result


def merged_in_current_extract() -> dict[str, dict]:
    """Partner -> latest successfully sent merge of the current extract (for the duplicate overview)."""
    result = {}
    for entry in reversed(history(limit=1000)):
        if entry["status"] == "sent" and entry["current_extract"]:
            for pid in entry["partners"]:
                result[pid] = {"id": entry["id"], "created_at": entry["created_at"], "keep": entry["keep"]}
    return result


def log_detail(entry_id: int) -> dict:
    with db() as con:
        r = con.execute("SELECT * FROM merges WHERE id = ?", (entry_id,)).fetchone()
    if r is None:
        raise KeyError(entry_id)
    return {**dict(r), **{k: json.loads(r[k] or "null") for k in ("partners", "actions", "choices", "items",
                                                                   "changes", "warnings")}}
