"""Load the SAP business partner extract (GPA_<timestamp>_<nr>_<TABLE>.txt in input/GPA) and build one search
document per business partner. Every table is keyed by PARTNER; the table names are described in objects.md."""
import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property, lru_cache

from ..config import PARTNER_INPUT_DIR
from ..tables import read_table, strip_zeros

TYPES = {"1": "Person", "2": "Organisation", "3": "Gruppe"}
TITLES = {"0001": "Frau", "0002": "Herr", "0003": "Firma", "0004": "Herr und Frau"}

# BP roles (G100-RLTYP) -> German label; others are shown with their code
ROLES = {
    "000000": "Geschäftspartner allgemein", "FLCU00": "Debitor (FI)", "FLCU01": "Kunde", "FLVN00": "Kreditor (FI)",
    "FLVN01": "Lieferant", "BUP001": "Kontaktperson", "BUP002": "Interessent", "BUP003": "Mitarbeiter",
    "BUP004": "Organisationseinheit", "UKM000": "Kreditmanagement", "UDM000": "Forderungsmanagement",
    "CRM000": "Auftraggeber", "CRM010": "Ansprechpartner", "BPSITE": "Standort",
}
# condensed role for badges and the role filter
ROLE_GROUPS = {"FLCU00": "Kunde", "FLCU01": "Kunde", "FLVN00": "Lieferant", "FLVN01": "Lieferant",
               "BUP001": "Kontaktperson", "CRM010": "Kontaktperson", "BUP002": "Interessent",
               "BUP003": "Mitarbeiter", "BUP004": "Organisationseinheit"}

COUNTRIES = {
    "DE": "Deutschland", "AT": "Österreich", "CH": "Schweiz", "US": "USA", "GB": "Großbritannien", "PL": "Polen",
    "IT": "Italien", "BE": "Belgien", "NL": "Niederlande", "FR": "Frankreich", "ES": "Spanien", "DK": "Dänemark",
    "SE": "Schweden", "FI": "Finnland", "NO": "Norwegen", "CZ": "Tschechien", "SK": "Slowakei", "HU": "Ungarn",
    "PT": "Portugal", "TR": "Türkei", "LU": "Luxemburg", "IE": "Irland", "SI": "Slowenien", "HR": "Kroatien",
    "RO": "Rumänien", "BG": "Bulgarien", "GR": "Griechenland", "CN": "China", "JP": "Japan", "IN": "Indien",
    "CA": "Kanada", "MX": "Mexiko", "BR": "Brasilien", "AU": "Australien", "KR": "Südkorea", "TW": "Taiwan",
    "SG": "Singapur", "RU": "Russland", "UA": "Ukraine", "LI": "Liechtenstein", "IL": "Israel", "ZA": "Südafrika",
}

CONTACT_RELATION = "BUR001"   # contact person (organisation <-> person)


def country_name(code: str) -> str:
    return COUNTRIES.get(code, code)


def table_labels() -> dict[str, str]:
    """GPA table name -> German description from input/GPA/objects.md ('G000<TAB>Allgemeine Daten')."""
    labels = {}
    for path in [PARTNER_INPUT_DIR / "objects.md", PARTNER_INPUT_DIR.parent / "objects.md"]:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                parts = re.split(r"\t+|\s{2,}|\s*\|\s*", line.strip().strip("|"), maxsplit=1)
                if len(parts) == 2 and re.fullmatch(r"G[A-Z0-9]{3}", parts[0]):
                    labels[parts[0]] = parts[1].strip()
            break
    return labels


def tables_in_extract() -> list[str]:
    """All GPA tables in the input folder, in extract order (G000 first)."""
    found = {}
    for path in sorted(PARTNER_INPUT_DIR.glob("*_G???.txt")):
        m = re.match(r".*_(\d+)_(G[A-Z0-9]{3})\.txt$", path.name)
        if m:
            found[m.group(2)] = int(m.group(1))
    return sorted(found, key=found.get)


def today() -> str:
    return date.today().strftime("%Y%m%d")


def valid(row: dict, start: str, end: str) -> bool:
    return (row.get(start) or "00000000") <= today() <= (row.get(end) or "99991231")


def phone(row: dict, number: str = "TEL_NUMBER", ext: str = "TEL_EXTENS") -> str:
    return row[number] + (f"-{row[ext]}" if row.get(ext) else "") if row.get(number) else ""


@dataclass
class Partner:
    partner: str                                   # zero padded, as in SAP
    g000: dict
    rows: dict[str, list[dict]] = field(default_factory=dict)       # all other tables, unchanged
    related: dict[str, "Partner"] = field(default_factory=dict, repr=False)   # all partners (for relations)
    materials: list[dict] = field(default_factory=list)            # purchasing info records (EINA) of the vendor

    # --- identification -------------------------------------------------
    @property
    def id(self) -> str:
        return strip_zeros(self.partner)

    @property
    def type(self) -> str:
        return self.g000.get("TYPE", "")

    @property
    def type_text(self) -> str:
        return TYPES.get(self.type, self.type)

    @cached_property
    def name(self) -> str:
        g = self.g000
        if self.type == "1":
            name = " ".join(p for p in (g.get("TITLE_ACA1"), g.get("NAME_FIRST"), g.get("NAMEMIDDLE"),
                                        g.get("PREFIX1"), g.get("NAME_LAST"), g.get("NAME_LAST2")) if p)
            return name or g.get("NAME1_TEXT", "")
        if self.type == "3":
            return " ".join(p for p in (g.get("NAME_GRP1"), g.get("NAME_GRP2")) if p)
        return " ".join(p for p in (g.get("NAME_ORG1"), g.get("NAME_ORG2"), g.get("NAME_ORG3"), g.get("NAME_ORG4"))
                        if p)

    @property
    def display_name(self) -> str:
        return self.name or f"Geschäftspartner {self.id}"

    @property
    def search_terms(self) -> list[str]:
        return [t for t in (self.g000.get("BU_SORT1"), self.g000.get("BU_SORT2")) if t]

    # --- roles, customer, vendor ----------------------------------------
    def table(self, name: str) -> list[dict]:
        return self.rows.get(name, [])

    @property
    def role_codes(self) -> list[str]:
        return list(dict.fromkeys(r["RLTYP"] for r in self.table("G100") if r.get("RLTYP")))

    @property
    def roles(self) -> list[str]:
        """Condensed roles: Kunde, Lieferant, Kontaktperson, ... (other roles with their label)."""
        return list(dict.fromkeys(ROLE_GROUPS.get(c) or ROLES.get(c, c) for c in self.role_codes
                                  if c != "000000"))

    @property
    def customer(self) -> dict:
        return next(iter(self.table("GKA1")), {})

    @property
    def vendor(self) -> dict:
        return next(iter(self.table("GLA1")), {})

    @property
    def kunnr(self) -> str:
        return strip_zeros(self.customer.get("KUNNR", ""))

    @property
    def lifnr(self) -> str:
        return strip_zeros(self.vendor.get("LIFNR", ""))

    @property
    def deleted(self) -> bool:
        """Archiving flag of the BP or deletion flag of the customer / vendor."""
        return "X" in (self.g000.get("XDELE"), self.customer.get("LOEVM"), self.vendor.get("LOEVM"))

    @property
    def blocks(self) -> list[str]:
        c, v = self.customer, self.vendor
        blocks = []
        if self.g000.get("XBLCK") == "X":
            blocks.append("Zentral gesperrt")
        if c.get("AUFSD"):
            blocks.append(f"Auftragssperre {c['AUFSD']}")
        if c.get("LIFSD"):
            blocks.append(f"Liefersperre {c['LIFSD']}")
        if c.get("FAKSD"):
            blocks.append(f"Fakturasperre {c['FAKSD']}")
        if c.get("SPERR") or v.get("SPERR"):
            blocks.append("Buchungssperre")
        if v.get("SPERM"):
            blocks.append("Einkaufssperre")
        return blocks

    # --- addresses and communication --------------------------------------
    @cached_property
    def addresses(self) -> list[dict]:
        """Addresses (GADC) with their communication data (GAD2/3/6/U) and usages (G021), default first."""
        def by_addr(table):
            grouped = defaultdict(list)
            for r in self.table(table):
                grouped[r.get("ADDRNUMBER")].append(r)
            return grouped
        phones, faxes, mails, urls, usages = (by_addr(t) for t in ("GAD2", "GAD3", "GAD6", "GADU", "G021"))
        result = []
        for r in sorted(self.table("GADC"), key=lambda r: r.get("XDFADR") != "X"):
            nr = r.get("ADDRNUMBER")
            street = " ".join(p for p in (r.get("STREET"), r.get("HOUSE_NUM1"), r.get("HOUSE_NUM2")) if p)
            city = " ".join(p for p in (r.get("POST_CODE1"), r.get("CITY1")) if p)
            if r.get("CITY2"):
                city += f" ({r['CITY2']})"
            po_box = ""
            if r.get("PO_BOX"):
                po_box = f"Postfach {r['PO_BOX']}" + (f", {r['POST_CODE2']}" if r.get("POST_CODE2") else "")
            lines = [p for p in (r.get("NAME_CO") and f"c/o {r['NAME_CO']}", street, po_box, city,
                                 country_name(r.get("COUNTRY", ""))) if p]
            result.append({
                "addrnumber": nr, "default": r.get("XDFADR") == "X", "street": street, "postcode": r.get("POST_CODE1", ""),
                "city": r.get("CITY1", ""), "district": r.get("CITY2", ""), "po_box": po_box,
                "country": r.get("COUNTRY", ""), "country_text": country_name(r.get("COUNTRY", "")),
                "region": r.get("REGION", ""), "language": r.get("LANGU", ""), "time_zone": r.get("TIME_ZONE", ""),
                "text": ", ".join(lines),
                "valid": f"{r.get('ADDR_VALID_FROM', '')} - {r.get('ADDR_VALID_TO', '')}",
                "phones": [{"number": phone(p), "international": p.get("TELNR_LONG", ""),
                            "mobile": p.get("R3_USER") == "3", "default": p.get("FLGDEFAULT") == "X"}
                           for p in phones.get(nr, []) if p.get("TEL_NUMBER")],
                "faxes": [{"number": phone(p, "FAX_NUMBER", "FAX_EXTENS"), "international": p.get("FAXNR_LONG", ""),
                           "default": p.get("FLGDEFAULT") == "X"} for p in faxes.get(nr, []) if p.get("FAX_NUMBER")],
                "emails": list(dict.fromkeys(p["SMTP_ADDR"] for p in mails.get(nr, []) if p.get("SMTP_ADDR"))),
                "urls": list(dict.fromkeys(p["URI_ADDR"] for p in urls.get(nr, []) if p.get("URI_ADDR"))),
                "usages": list(dict.fromkeys(u["ADR_KIND"] for u in usages.get(nr, []) if u.get("ADR_KIND"))),
            })
        return result

    @property
    def address(self) -> dict:
        return self.addresses[0] if self.addresses else {}

    @property
    def emails(self) -> list[str]:
        return list(dict.fromkeys(e for a in self.addresses for e in a["emails"]))

    @property
    def phones(self) -> list[dict]:
        return [p for a in self.addresses for p in a["phones"]]

    @property
    def urls(self) -> list[str]:
        return list(dict.fromkeys(u for a in self.addresses for u in a["urls"]))

    # --- relationships ----------------------------------------------------
    @cached_property
    def relations(self) -> list[dict]:
        """Relationships (G050) with the related partner and the contact person data (G050, GRAC, GRA2/3/6)."""
        def key(r):
            return r.get("PARTNER2"), r.get("RELTYP"), r.get("DIRECTION"), r.get("LFDNR")
        extra = defaultdict(lambda: defaultdict(list))
        for table in ("GRAC", "GRA2", "GRA3", "GRA6"):
            for r in self.table(table):
                extra[key(r)][table].append(r)
        result = []
        for r in self.table("G050"):
            other = self.related.get(strip_zeros(r.get("PARTNER2", "")))
            e = extra[key(r)]
            grac = next(iter(e["GRAC"]), {})
            if r.get("RELTYP") == CONTACT_RELATION:
                # the person is the contact person, the organisation has the contact person
                label = "Ansprechpartner bei" if self.type == "1" else "Ansprechpartner"
            else:
                label = f"Beziehung {r.get('RELTYP', '')}"
            phones = [p for p in dict.fromkeys([phone(r)] + [phone(p) for p in e["GRA2"]]) if p]
            result.append({
                "partner": strip_zeros(r.get("PARTNER2", "")),
                "name": other.display_name if other else "",
                "type": other.type if other else "",
                "type_text": other.type_text if other else "",
                "reltyp": r.get("RELTYP", ""), "direction": r.get("DIRECTION", ""), "label": label,
                "function": r.get("FNCTN") or grac.get("FUNCTION", ""), "function_code": r.get("PAFKT", ""),
                "department": r.get("DPRTMNT") or grac.get("DEPARTMENT", ""), "department_code": r.get("ABTNR", ""),
                "phones": phones,
                "faxes": [p for p in dict.fromkeys([phone(r, "FAX_NUMBER", "FAX_EXTENS")]
                                                   + [phone(p, "FAX_NUMBER", "FAX_EXTENS") for p in e["GRA3"]]) if p],
                "emails": [m for m in dict.fromkeys([r.get("SMTP_ADDRESS", "")]
                                                    + [p.get("SMTP_ADDR", "") for p in e["GRA6"]]) if m],
                "vip": r.get("PAVIP", ""), "default": r.get("XDFREL") == "X",
                "valid_from": r.get("DATE_FROM", ""), "valid_to": r.get("DATE_TO", ""),
                "active": valid(r, "DATE_FROM", "DATE_TO"),
                "other_deleted": bool(other and other.deleted),
            })
        result.sort(key=lambda x: (not x["active"], x["reltyp"] != CONTACT_RELATION, x["name"]))
        return result

    @property
    def contacts(self) -> list[dict]:
        """Contact persons of an organisation / organisations a person is the contact of (active only)."""
        return [r for r in self.relations if r["reltyp"] == CONTACT_RELATION and r["active"]]

    # --- tax, bank, ids -----------------------------------------------------
    @property
    def tax_numbers(self) -> list[str]:
        return list(dict.fromkeys(r["TAXNUM"] for r in self.table("GTAX") if r.get("TAXNUM")))

    @property
    def ibans(self) -> list[str]:
        return list(dict.fromkeys(r["IBAN"] for r in self.table("G0BK") if r.get("IBAN")))

    @property
    def id_numbers(self) -> list[str]:
        return list(dict.fromkeys(r["IDNUMBER"] for r in self.table("G0ID") if r.get("IDNUMBER")))

    # --- search -------------------------------------------------------------
    def describe_relation(self, r: dict) -> str:
        details = [d for d in (r["function"], r["department"] and f"Abteilung {r['department']}") if d]
        return r["name"] + (f" ({', '.join(details)})" if details else "")

    def document(self, context: bool = True) -> str:
        """Text that is embedded for the semantic search. With context: contact persons / companies and the materials
        supplied (purchasing info records), so that 'Lieferant für Kabelverschraubungen' finds the vendor."""
        g = self.g000
        head = [self.display_name]
        if self.search_terms:
            head.append("Suchbegriff: " + " ".join(self.search_terms))
        lines = [" | ".join(head)]
        kind = self.type_text
        if self.type == "1" and TITLES.get(g.get("TITLE", "")) in ("Frau", "Herr"):
            kind += f" ({TITLES[g['TITLE']]})"
        lines.append(f"{kind}" + (f", Rollen: {', '.join(self.roles)}" if self.roles else ""))
        for a in self.addresses[:2]:
            lines.append(("Adresse: " if a["default"] else "Weitere Adresse: ") + a["text"])
        comm = self.emails[:3] + self.urls[:2]
        if comm:
            lines.append("Kommunikation: " + ", ".join(comm))
        if self.kunnr:
            lines.append(f"Kunde {self.kunnr} (Kontengruppe {self.customer.get('KTOKD', '')})")
        if self.lifnr:
            lines.append(f"Lieferant {self.lifnr} (Kontengruppe {self.vendor.get('KTOKK', '')})")
        if self.blocks or self.deleted:
            lines.append("Status: " + ", ".join(self.blocks + (["Löschvormerkung"] if self.deleted else [])))
        if context:
            contacts = [self.describe_relation(r) for r in self.contacts if r["name"]]
            if contacts:
                label = "Ansprechpartner bei" if self.type == "1" else "Ansprechpartner"
                lines.append(f"{label}: " + "; ".join(contacts[:15]))
            others = [f"{r['label']}: {r['name']}" for r in self.relations
                      if r["reltyp"] != CONTACT_RELATION and r["active"] and r["name"]]
            if others:
                lines.append("; ".join(others[:10]))
            if self.materials:
                lines.append(self.supplied_materials())
        return "\n".join(lines)[:6000]

    def supplied_materials(self, sample: int = 25) -> str:
        """'Liefert Materialien (n): classes; short texts' -- an evenly spread sample of the (alphabetically sorted)
        short texts, so that vendors with thousands of info records are described by all their product kinds."""
        texts = sorted(dict.fromkeys(m["text"] for m in self.materials if m["text"]), key=str.lower)
        if len(texts) > sample:
            texts = [texts[i * len(texts) // sample] for i in range(sample)]
        classes = Counter(c for m in self.materials for c in m.get("classes", []))
        line = f"Liefert Materialien ({len(self.materials)}): "
        if classes:
            line += "Klassen " + ", ".join(c for c, _ in classes.most_common(8)) + "; "
        return line + "; ".join(texts)

    def search_text(self) -> str:
        """Text for the lexical / fuzzy search: names, address, communication and all numbers. Without the context
        (contact names, materials), otherwise every company would match the names of its contact persons."""
        a = self.address
        phones = [p["number"] for p in self.phones]
        return " ".join([
            self.document(context=False), self.id, self.kunnr, self.lifnr, self.g000.get("BPEXT", ""),
            *self.tax_numbers, *self.ibans, *self.id_numbers, *phones, a.get("country", ""), a.get("region", ""),
            self.g000.get("BU_GROUP", ""),
        ])

    def search_keys(self) -> list[tuple[str, list[str]]]:
        """Exact keys: (label, values)."""
        phones = [v for p in self.phones for v in (p["number"], p["international"])]
        faxes = [v for a in self.addresses for p in a["faxes"] for v in (p["number"], p["international"])]
        urls = [u for url in self.urls for u in (url, re.sub(r"^(https?://)?(www\.)?", "", url))]
        keys = [("Partnernummer", [self.id, self.partner]), ("Kundennummer", [self.kunnr]),
                ("Lieferantennummer", [self.lifnr]), ("Ext. Partnernummer", [self.g000.get("BPEXT", "")]),
                ("Steuernummer", self.tax_numbers), ("IBAN", self.ibans), ("Identifikationsnr.", self.id_numbers),
                ("E-Mail", self.emails), ("Telefon", phones), ("Fax", faxes), ("Webseite", urls)]
        if self.type == "1":   # business e-mail / phone of a contact person are stored with the relationship
            keys += [("E-Mail (Ansprechpartner)", [m for r in self.contacts for m in r["emails"]]),
                     ("Telefon (Ansprechpartner)", [p for r in self.contacts for p in r["phones"]])]
        return keys

    def index_metadata(self) -> dict:
        """Metadata stored with the embedding in the vector DB."""
        return {"type": self.type, "country": self.address.get("country", ""), "group": self.g000.get("BU_GROUP", "")}

    def content_hash(self) -> str:
        return hashlib.sha1(self.document().encode("utf-8")).hexdigest()[:16]

    # --- API ------------------------------------------------------------------
    def summary(self) -> dict:
        a = self.address
        default_phone = next((p for p in self.phones if p["default"]), self.phones[0] if self.phones else {})
        return {
            "partner": self.id,
            "name": self.display_name,
            "type": self.type,
            "type_text": self.type_text,
            "group": self.g000.get("BU_GROUP", ""),
            "search_term": " ".join(self.search_terms),
            "roles": self.roles,
            "kunnr": self.kunnr,
            "lifnr": self.lifnr,
            "street": a.get("street", ""),
            "postcode": a.get("postcode", ""),
            "city": a.get("city", ""),
            "country": a.get("country", ""),
            "country_text": a.get("country_text", ""),
            "address": a.get("text", ""),
            "email": self.emails[0] if self.emails else "",
            "phone": default_phone.get("number", ""),
            "tax_number": self.tax_numbers[0] if self.tax_numbers else "",
            "deleted": self.deleted,
            "blocked": bool(self.blocks),
            "blocks": self.blocks,
            "contacts": len(self.contacts),
            "materials": len(self.materials),
        }

    def llm_view(self, materials: int = 20) -> dict:
        """The attributes of a partner that matter for the AI search and the duplicate check."""
        g = self.g000
        data = {
            "partner": self.id, "name": self.display_name, "type": self.type_text,
            "title": TITLES.get(g.get("TITLE", "")), "search_terms": self.search_terms,
            "grouping": g.get("BU_GROUP"), "roles": [ROLES.get(c, c) for c in self.role_codes],
            "birthdate": g.get("BIRTHDT"), "legal_form": g.get("LEGAL_ENTY"), "founded": g.get("FOUND_DAT"),
            "addresses": [a["text"] for a in self.addresses],
            "region": self.address.get("region"), "language": g.get("BU_LANGU") or self.address.get("language"),
            "phones": [p["number"] for p in self.phones][:5],
            "faxes": [f["number"] for a in self.addresses for f in a["faxes"]][:3],
            "emails": self.emails[:5], "websites": self.urls[:3],
            "customer": ({"number": self.kunnr, "account_group": self.customer.get("KTOKD"),
                          "annual_sales": (f"{self.customer['UMSA1']} {self.customer.get('UWAER', '')} "
                                           f"({self.customer.get('UMJAH', '')})" if self.customer.get("UMSA1") else None)}
                         if self.kunnr else None),
            "vendor": {"number": self.lifnr, "account_group": self.vendor.get("KTOKK")} if self.lifnr else None,
            "tax_numbers": [f"{r.get('TAXTYPE')}: {r.get('TAXNUM')}" for r in self.table("GTAX")],
            "id_numbers": [f"{r.get('TYPE')}: {r.get('IDNUMBER')}" for r in self.table("G0ID")],
            "bank_accounts": [r.get("IBAN") or f"{r.get('BANKS')} {r.get('BANKL')} {r.get('BANKN')}"
                              for r in self.table("G0BK")],
            "relations": [{"label": r["label"], "partner": r["partner"], "name": r["name"],
                           "function": r["function"] or None, "department": r["department"] or None,
                           "emails": r["emails"] or None} for r in self.relations if r["active"]][:15],
            "materials_supplied": self.supplied_materials(materials) if self.materials else None,
            "blocks": self.blocks, "deleted": self.deleted,
        }
        clean = lambda d: {k: v for k, v in d.items() if v not in (None, "", [], {})}   # noqa: E731
        data["relations"] = [clean(r) for r in data["relations"]]
        if data["customer"]:
            data["customer"] = clean(data["customer"])
        return clean(data)

    def tables(self) -> dict[str, list[dict]]:
        """All rows of the partner per SAP table, unchanged as in the extract."""
        return {"G000": [self.g000], **{t: rows for t, rows in self.rows.items() if rows}}

    def detail(self) -> dict:
        g = self.g000
        return {
            **self.summary(),
            "title": TITLES.get(g.get("TITLE", ""), g.get("TITLE", "")),
            "birthdate": g.get("BIRTHDT", ""),
            "language": g.get("BU_LANGU") or g.get("LANGU_CORR", ""),
            "general": {k: v for k, v in g.items() if v},
            "role_rows": [{**r, "label": ROLES.get(r["RLTYP"], "")} for r in self.table("G100")],
            "addresses": self.addresses,
            "relations": self.relations,
            "customer": {k: v for k, v in self.customer.items() if v},
            "vendor": {k: v for k, v in self.vendor.items() if v},
            "bank": self.table("G0BK"),
            "tax": self.table("GTAX"),
            "ids": self.table("G0ID"),
            "material_list": self.materials,
            "tables": [{"table": t, "rows": rows} for t, rows in self.tables().items()],
            "document": self.document(),
        }


def vendor_materials() -> dict[str, list[dict]]:
    """Vendor number (without zeros) -> materials with a purchasing info record (EINA) of that vendor."""
    try:
        from ..material.data import load_materials
        materials = load_materials()
    except (FileNotFoundError, OSError):   # no material extract: partners without material context
        return {}
    result: dict[str, list[dict]] = defaultdict(list)
    for m in materials.values():
        for r in m.eina:
            if r.get("LIFNR"):
                result[strip_zeros(r["LIFNR"])].append({
                    "matnr": m.id, "text": m.short_text, "idnlf": r.get("IDNLF", ""), "infnr": r.get("INFNR", ""),
                    "mtart": m.mara.get("MTART", ""), "classes": m.classes,
                    "deleted": m.deleted or r.get("LOEKZ") == "X",
                })
    return result


@lru_cache(maxsize=1)
def load_partners() -> dict[str, Partner]:
    """All business partners keyed by display partner number (without leading zeros)."""
    tables = tables_in_extract()
    if "G000" not in tables:
        raise FileNotFoundError(f"Keine Datei *_G000.txt in {PARTNER_INPUT_DIR}")
    by_partner: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for table in tables:
        if table == "G000":
            continue
        for r in read_table(PARTNER_INPUT_DIR, table):
            if r.get("XDELE") != "X":
                by_partner[r.get("PARTNER", "")][table].append(r)

    partners: dict[str, Partner] = {}
    for g in read_table(PARTNER_INPUT_DIR, "G000"):
        p = Partner(partner=g["PARTNER"], g000=g, rows=dict(by_partner.get(g["PARTNER"], {})), related=partners)
        partners[p.id] = p

    supplied = vendor_materials()
    for p in partners.values():
        if p.lifnr:
            p.materials = sorted(supplied.get(p.lifnr, []), key=lambda m: m["matnr"])
    return partners
