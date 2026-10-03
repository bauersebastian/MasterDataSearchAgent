"""Load the SAP material master extract from the input folder and build one search document per material."""
import csv
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache

from .config import EMBED_LANGUAGES, FALLBACK_LANGUAGES, INPUT_DIR, LANGUAGE

TABLES = ["MARA", "MAKT", "MARM", "MECL", "MEME", "MTXT", "EINA"]
BOM_TABLES = ["STKO", "STPO"]   # bill of material header / items, keyed by the header material (optional files)

# long text IDs -> label in the embedded document
TEXT_IDS = {"GRUN": "Grunddatentext", "BEST": "Bestelltext", "IVER": "Interner Vermerk", "0001": "Vertriebstext"}


def strip_matnr(matnr: str) -> str:
    """SAP internal (zero padded) material number -> display form."""
    return (matnr.lstrip("0") or "0") if matnr.isdigit() else matnr


def pad_matnr(matnr: str) -> str:
    matnr = matnr.strip()
    return matnr.zfill(18) if matnr.isdigit() else matnr.upper()


def table_file(table: str):
    files = sorted(INPUT_DIR.glob(f"*_{table}.txt"))
    if not files:
        raise FileNotFoundError(f"Keine Datei *_{table}.txt in {INPUT_DIR}")
    return files[-1]   # newest extract


def read_table(table: str, optional: bool = False) -> list[dict]:
    if optional and not any(INPUT_DIR.glob(f"*_{table}.txt")):
        return []
    with open(table_file(table), encoding="utf-8", newline="") as fh:
        rows = csv.DictReader(fh, delimiter=";", quoting=csv.QUOTE_NONE)
        return [{k: (v or "").strip() for k, v in row.items() if k} for row in rows]


def number(value: str) -> str:
    """'200,000' / '10.000' -> '200' / '10' for readable documents."""
    v = value.replace(",", ".")
    try:
        f = float(v)
    except ValueError:
        return value
    return f"{f:g}".replace(".", ",")


@dataclass
class Material:
    matnr: str                      # zero padded, as in SAP
    mara: dict
    texts: dict[str, str]           # language -> short text
    makt: list[dict] = field(default_factory=list)
    marm: list[dict] = field(default_factory=list)
    mecl: list[dict] = field(default_factory=list)
    meme: list[dict] = field(default_factory=list)
    mtxt: list[dict] = field(default_factory=list)
    eina: list[dict] = field(default_factory=list)
    stko: list[dict] = field(default_factory=list)     # BOMs of this material (as header)
    stpo: list[dict] = field(default_factory=list)     # items of these BOMs
    bom_components: list[str] = field(default_factory=list)   # short texts of the direct components
    bom_parents: list[str] = field(default_factory=list)      # short texts of the BOM headers using this material

    @property
    def id(self) -> str:
        return strip_matnr(self.matnr)

    @property
    def short_text(self) -> str:
        for lang in [LANGUAGE, *FALLBACK_LANGUAGES]:
            if self.texts.get(lang):
                return self.texts[lang]
        return next(iter(self.texts.values()), "")

    @property
    def eans(self) -> list[str]:
        eans = [self.mara.get("EAN11", "")] + [r.get("EAN11", "") for r in self.marm]
        return list(dict.fromkeys(e for e in eans if e))

    @property
    def vendor_parts(self) -> list[str]:
        return list(dict.fromkeys(r["IDNLF"] for r in self.eina if r.get("IDNLF")))

    @property
    def vendors(self) -> list[str]:
        return list(dict.fromkeys(strip_matnr(r["LIFNR"]) for r in self.eina if r.get("LIFNR")))

    @property
    def classes(self) -> list[str]:
        return list(dict.fromkeys(r["CLASS_BEZEI"] or r["CLASS"] for r in self.mecl))

    @property
    def deleted(self) -> bool:
        return self.mara.get("LVORM") == "X"

    def long_texts(self) -> dict[str, str]:
        """(text ID, language) lines joined to one text per text ID (own language first)."""
        texts: dict[str, list[str]] = defaultdict(list)
        chosen: dict[str, tuple] = {}   # one language / sales area per text ID is enough
        for r in sorted(self.mtxt, key=lambda r: (r["TDSPRAS"] != LANGUAGE, r["TDID"], r["TDSPRAS"], r["ZEILE"])):
            variant = (r["TDSPRAS"], r.get("VKORG", ""), r.get("VTWEG", ""))
            if chosen.setdefault(r["TDID"], variant) == variant and r["TDLINE"]:
                texts[r["TDID"]].append(r["TDLINE"])
        return {tdid: " ".join(lines) for tdid, lines in texts.items()}

    def features(self) -> list[tuple[str, str]]:
        """(feature name, readable value); ETIM value codes (EV...) carry no meaning for the search."""
        result = []
        for r in self.meme:
            value = r["ATWRT"]
            if re.fullmatch(r"EV\d{6}", value):
                continue
            value = "ja" if value == "X" else number(value)
            result.append((r["ATNAM_BEZEI"] or r["ATNAM"], value))
        return result

    def document(self, bom: bool = True) -> str:
        """Text that is embedded for the semantic search (with the names of BOM components / parent BOMs)."""
        parts = [self.short_text]
        for lang in EMBED_LANGUAGES:
            text = self.texts.get(lang)
            if text and text not in parts:
                parts.append(text)
        lines = [" | ".join(parts)]
        for tdid, text in self.long_texts().items():
            lines.append(f"{TEXT_IDS.get(tdid, tdid)}: {text}")
        if self.classes:
            lines.append("Klasse: " + ", ".join(self.classes))
        if feats := self.features():
            lines.append("Merkmale: " + "; ".join(f"{n}: {v}" for n, v in feats))
        if bom and self.bom_components:
            lines.append("Stückliste aus: " + "; ".join(self.bom_components[:25]))
        if bom and self.bom_parents:
            lines.append("Verwendet in Stückliste: " + "; ".join(self.bom_parents[:15]))
        return "\n".join(lines)[:6000]

    def search_text(self) -> str:
        """Text for the lexical / fuzzy search: everything a user may type, incl. numbers and codes. Without the BOM
        context, otherwise every assembly would match the words of its components."""
        return " ".join([
            self.document(bom=False), *self.texts.values(), self.id, *self.eans, *self.vendor_parts,
            self.mara.get("BISMT", ""), self.mara.get("MATKL", ""), self.mara.get("MTART", ""),
        ])

    def content_hash(self) -> str:
        return hashlib.sha1(self.document().encode("utf-8")).hexdigest()[:16]

    def summary(self) -> dict:
        m = self.mara
        return {
            "matnr": self.id,
            "text": self.short_text,
            "mtart": m.get("MTART", ""),
            "matkl": m.get("MATKL", ""),
            "meins": m.get("MEINS", ""),
            "ean": m.get("EAN11", "") or (self.eans[0] if self.eans else ""),
            "vendors": self.vendors,
            "vendor_parts": self.vendor_parts,
            "classes": self.classes,
            "status": m.get("MSTAE", ""),
            "deleted": self.deleted,
            "bom": bool(self.stko),
            "used_in": len(self.bom_parents),
        }

    def tables(self) -> dict[str, list[dict]]:
        """All rows of the material per SAP table, unchanged as in the extract."""
        return {"MARA": [self.mara], "MAKT": self.makt, "MARM": self.marm, "MECL": self.mecl, "MEME": self.meme,
                "MTXT": self.mtxt, "EINA": self.eina, "STKO": self.stko, "STPO": self.stpo}

    def detail(self) -> dict:
        return {
            **self.summary(),
            "texts": [{"SPRAS": k, "MAKTX": v} for k, v in self.texts.items()],
            "long_texts": [{"TDID": k, "label": TEXT_IDS.get(k, k), "text": v} for k, v in self.long_texts().items()],
            "mara": {k: v for k, v in self.mara.items() if v},
            "marm": self.marm,
            "mecl": self.mecl,
            "meme": self.meme,
            "eina": [{**r, "LIFNR": strip_matnr(r["LIFNR"])} for r in self.eina],
            "document": self.document(),
        }


@lru_cache(maxsize=1)
def load_materials() -> dict[str, Material]:
    """All materials keyed by display material number (without leading zeros)."""
    rows = {t: read_table(t) for t in TABLES} | {t: read_table(t, optional=True) for t in BOM_TABLES}
    by_matnr: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for table in TABLES[1:] + BOM_TABLES:
        for r in rows[table]:
            if r.get("XDELE") != "X":
                by_matnr[r["MATNR"]][table].append(r)

    materials = {}
    for mara in rows["MARA"]:
        sub = by_matnr.get(mara["MATNR"], {})
        texts = {}
        for r in sub.get("MAKT", []):
            if r["MAKTX"] and r["SPRAS"] not in texts:
                texts[r["SPRAS"]] = r["MAKTX"]
        material = Material(matnr=mara["MATNR"], mara=mara, texts=texts, makt=sub.get("MAKT", []),
                            marm=sub.get("MARM", []),
                            mecl=sub.get("MECL", []), meme=sub.get("MEME", []), mtxt=sub.get("MTXT", []),
                            eina=sub.get("EINA", []), stko=sub.get("STKO", []), stpo=sub.get("STPO", []))
        materials[material.id] = material

    # direct BOM relations for the search documents (the full graph is in bom.py)
    for header in materials.values():
        for item in header.stpo:
            component = materials.get(strip_matnr(item.get("IDNRK", "")))
            if component is None or component is header:
                continue
            if component.short_text not in header.bom_components:
                header.bom_components.append(component.short_text)
            if header.short_text not in component.bom_parents:
                component.bom_parents.append(header.short_text)
    return materials
