"""Duplicate check for one business partner: candidates from semantic neighbours, fuzzy name similarity (without the
legal form), BM25 over name and city, identical addresses and shared keys (tax number, IBAN, e-mail, phone, website,
ID number, customer / vendor cross references); optional assessment of the candidates by the LLM."""
import json
import re
from collections import defaultdict
from functools import lru_cache
from typing import Literal, Optional

from pydantic import BaseModel, Field
from rapidfuzz import fuzz, process

from .. import llm, vectordb
from ..config import DUPLICATE_CANDIDATES, MODEL, PARTNER_COLLECTION, RRF_K
from ..textsearch import normalize
from .data import Partner, load_partners
from .search import index

# keys that identify a partner (own address data, not the contact data of relationships)
STRONG_KEYS = ("Steuernummer", "IBAN", "Identifikationsnr.", "Ext. Partnernummer")
WEAK_KEYS = ("E-Mail", "Telefon", "Fax", "Webseite")

LEGAL_FORMS = re.compile(
    r"\b(gmbh|mbh|ag|kg|kgaa|ohg|gbr|ug|e\s?k|e\s?v|co|und|u|se|ltd|limited|inc|corp|corporation|llc|plc|sa|sas|sarl|"
    r"srl|spa|bv|nv|ab|as|aps|oy|sp\s?z\s?o\s?o|s\s?r\s?o|a\s?s|gesellschaft|haftungsbeschrankt)\b")


def core_name(name: str) -> str:
    """Name without legal form and punctuation: 'Müller GmbH & Co.KG' -> 'mueller'."""
    text = re.sub(r"[^a-z0-9 ]", " ", normalize(name))
    stripped = re.sub(r"\s+", " ", LEGAL_FORMS.sub(" ", text)).strip()
    return stripped or re.sub(r"\s+", " ", text).strip()


def address_key(p: Partner) -> str:
    a = p.address
    if not a.get("street") or not a.get("postcode"):
        return ""
    street = re.sub(r"str(asse|\.|\b)", "str", normalize(a["street"]))
    return re.sub(r"[^a-z0-9]", "", f"{a.get('country')}{a['postcode']}{street}")


@lru_cache(maxsize=1)
def lookup() -> tuple[list[str], list[str], dict[str, list[str]]]:
    """(partner ids, core names, address key -> partner ids) for the comparison."""
    partners = load_partners()
    by_address = defaultdict(list)
    for p in partners.values():
        if key := address_key(p):
            by_address[key].append(p.id)
    return list(partners), [core_name(p.display_name) for p in partners.values()], by_address


def name_similarity(a: Partner, b: Partner) -> float:
    return fuzz.token_sort_ratio(core_name(a.display_name), core_name(b.display_name)) / 100


def candidates(partner: str, limit: int = DUPLICATE_CANDIDATES) -> dict:
    partners = load_partners()
    p = partners.get(partner)
    if p is None:
        raise KeyError(partner)
    idx = index()
    pool = limit * 3
    fused: dict[str, float] = defaultdict(float)
    signals: dict[str, dict] = defaultdict(lambda: defaultdict(list))

    for rank, (pid, sim) in enumerate(vectordb.neighbours(PARTNER_COLLECTION, p.id, pool)):
        fused[pid] += 1 / (RRF_K + rank + 1)
        signals[pid]["semantic"] = round(sim, 3)

    ids, names, by_address = lookup()
    own_name = core_name(p.display_name)
    if own_name:
        found = process.extract(own_name, names, scorer=fuzz.token_set_ratio, limit=pool + 1, score_cutoff=60)
        for rank, (_, score, pos) in enumerate(n for n in found if ids[n[2]] != p.id):
            fused[ids[pos]] += 1 / (RRF_K + rank + 1)
        query = " ".join(x for x in (p.display_name, p.address.get("city", "")) if x)
        lexical, _, _ = idx.search(query, pool + 1)
        for rank, (doc, score) in enumerate(d for d in lexical if idx.ids[d[0]] != p.id):
            fused[idx.ids[doc]] += 1 / (RRF_K + rank + 1)
            signals[idx.ids[doc]]["lexical"] = round(score, 3)

    # identical address (street, house number, postcode, country)
    for pid in by_address.get(address_key(p), []):
        if pid != p.id:
            fused[pid] += 0.5
            signals[pid]["shared"].append("gleiche Adresse")

    # shared keys: tax number / IBAN / ID number are strong, communication data weaker (shared info@ addresses);
    # one signal per key type (a phone number is stored national and international)
    for label, values in p.search_keys():
        if label not in STRONG_KEYS + WEAK_KEYS:
            continue
        for value in values:
            for doc, found_as in idx.exact(value):
                pid = idx.ids[doc]
                if pid == p.id or found_as != label or any(t.startswith(label + " ") for t in signals[pid]["shared"]):
                    continue
                fused[pid] += 1 if label in STRONG_KEYS else 0.3
                signals[pid]["shared"].append(f"{label} {value}")
                if label in STRONG_KEYS:
                    signals[pid]["strong"] = True

    # customer <-> vendor cross references (KNA1-LIFNR / LFA1-KUNNR) pointing to another partner
    refs = {p.customer.get("LIFNR", "").lstrip("0"): "Kunde verweist auf Lieferant",
            p.vendor.get("KUNNR", "").lstrip("0"): "Lieferant verweist auf Kunde"}
    for other in partners.values():
        if other.id == p.id:
            continue
        for number, text in refs.items():
            if number and number in (other.lifnr, other.kunnr):
                fused[other.id] += 1
                signals[other.id]["shared"].append(f"{text} {number}")
                signals[other.id]["strong"] = True

    # persons: same name and birth date
    birth = p.g000.get("BIRTHDT")
    related = {r["partner"] for r in p.relations}
    result = []
    for pid, _ in sorted(fused.items(), key=lambda x: -x[1])[:limit]:
        c = partners[pid]
        s = signals[pid]
        name = name_similarity(p, c)
        if birth and birth == c.g000.get("BIRTHDT") and name >= 0.85:
            s["shared"].append(f"Geburtsdatum {birth}")
            s["strong"] = True
        score = max(name, s.get("semantic", 0))
        # same name at another place is usually a branch / site: lower, the LLM decides
        other_place = bool(p.address.get("city") and c.address.get("city")) \
            and core_name(p.address["city"]) != core_name(c.address["city"])
        if other_place:
            score *= 0.85
        if s.get("strong"):
            score = max(score, 0.95)
        elif "gleiche Adresse" in s["shared"] and name >= 0.8:
            score = max(score, 0.9)
        result.append({**c.summary(), "name_similarity": round(name, 3), "semantic": s.get("semantic"),
                       "lexical": s.get("lexical"), "shared": s["shared"],
                       "same_type": c.type == p.type, "related": pid in related, "other_place": other_place,
                       "score": round(score * 100)})
    result.sort(key=lambda c: -c["score"])
    return {"partner": p.summary(), "candidates": result,
            "semantic_available": vectordb.indexed_count(PARTNER_COLLECTION) > 0}


# --- LLM assessment -------------------------------------------------------

class Verdict(BaseModel):
    partner: str = Field(description="Partner number of the candidate")
    verdict: Literal["Dublette", "Mögliche Dublette", "Verwandt", "Verschieden"] = Field(
        description="Dublette: the same legal entity / person maintained twice; Mögliche Dublette: probably the same, "
                    "but the data is not conclusive; Verwandt: a different but related partner (branch / site of the "
                    "same company, other company of the same group, contact person of the partner); "
                    "Verschieden: unrelated partner")
    confidence: float = Field(description="Confidence of the verdict between 0 and 1")
    reason: str = Field(description="Short reason in German, naming the decisive attributes")


class DuplicateAssessment(BaseModel):
    summary: str = Field(description="One or two sentences in German: is the partner a duplicate, and of which?")
    verdicts: list[Verdict]


SYSTEM_PROMPT = """You are an SAP business partner master data steward checking for duplicate business partners
(organisations, persons, groups with their roles customer / vendor, addresses, communication, bank and tax data).
Compare the reference partner with each candidate and decide whether they are the same legal entity or person.

Rules:
- Identical tax number (VAT ID), IBAN or identification number (e.g. commercial register number) is strong evidence
  for the same legal entity. If name and address are completely different nevertheless, it is rather a data error
  (copied or test bank details): verdict "Mögliche Dublette" and say that the key should be checked. A customer / vendor number of the candidate referenced by the reference partner (or vice
  versa) means both records stand for the same company: in SAP S/4HANA one business partner should carry both
  the customer and the vendor role -- verdict "Dublette" unless the data clearly says otherwise.
- The same name (ignoring legal form spelling and abbreviations such as "GmbH & Co. KG" / "GmbH & Co.KG",
  "Str." / "Straße") at the same address is a duplicate.
- The same company name at a different address is usually another branch / site ("Niederlassung"): verdict
  "Verwandt" -- unless one record looks like an outdated copy (deletion flag, no roles, same communication data),
  then "Mögliche Dublette". Different legal entities of a group (e.g. "BMW AG" and "BMW Bank GmbH") are "Verwandt".
- Persons: the same first and last name with the same birth date, address or e-mail is a duplicate; the same name
  only is a "Mögliche Dublette". A person that is a contact person of the reference organisation is "Verwandt".
- A generic e-mail address or phone number shared by many records (e.g. info@ of the own company in test data) is
  weak evidence on its own.
- Test or placeholder partners ("Test", "Neuer GP", "Muster") are only duplicates if the data is really identical.
- A record of a different partner type (organisation vs. person) with the same name can be a wrongly created
  duplicate -- judge by address and communication data.
- Base your verdict only on the given data. Reply in German.
"""


def assess(partner: str, candidate_ids: Optional[list[str]] = None) -> dict:
    """Let the LLM judge the candidates (default: the heuristic candidates)."""
    partners = load_partners()
    if partner not in partners:
        raise KeyError(partner)
    if not candidate_ids:
        candidate_ids = [c["partner"] for c in candidates(partner)["candidates"]]
    candidate_ids = [c for c in candidate_ids if c in partners and c != partner][:DUPLICATE_CANDIDATES]
    if not candidate_ids:
        return {"summary": "Keine Kandidaten gefunden.", "verdicts": []}

    view = lambda pid: json.dumps(partners[pid].llm_view(materials=10), ensure_ascii=False)   # noqa: E731
    user = ("Reference partner:\n" + view(partner) + "\n\nCandidates:\n" + "\n".join(view(c) for c in candidate_ids))
    result = llm.parse(SYSTEM_PROMPT, user, DuplicateAssessment)
    verdicts = [v for v in result.verdicts if (v.partner.lstrip("0") or v.partner) in candidate_ids]
    for v in verdicts:
        v.partner = v.partner.lstrip("0") or v.partner
    return {"summary": result.summary, "verdicts": [v.model_dump() for v in verdicts], "model": MODEL}
