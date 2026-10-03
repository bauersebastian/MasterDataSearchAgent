"""Bill of material relations from STKO / STPO: multi-level BOM explosion, where-used list and graph data."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from .data import load_materials, number, strip_matnr

USAGES = {"1": "Fertigung", "2": "Konstruktion", "3": "Universal", "4": "Instandhaltung", "5": "Vertrieb",
          "6": "Kalkulation"}
ITEM_CATEGORIES = {"L": "Lagerposition", "N": "Nichtlagerposition", "T": "Textposition", "K": "Klassenposition",
                   "R": "Rohmaßposition", "D": "Dokumentposition", "I": "PM-Strukturelement"}
MAX_DEPTH = 15          # safety net; the extract has 4 levels
MAX_GRAPH_NODES = 150


@dataclass(frozen=True)
class Link:
    parent: str             # header material
    child: Optional[str]    # component material (None for text items)
    stlan: str              # BOM usage
    stlal: str              # alternative
    werks: str              # plant of the BOM (empty: plant independent)
    base: str               # base quantity of the BOM, e.g. "1 ST"
    posnr: str
    postp: str
    menge: str
    meins: str
    text: str               # item text (text items)

    @property
    def bom(self) -> str:
        return f"{self.stlan} {USAGES.get(self.stlan, '')} / Alt. {self.stlal}" + (f" / Werk {self.werks}" if self.werks else "")

    def matches(self, stlan: str, werks: str) -> bool:
        return (not stlan or self.stlan == stlan) and (not werks or self.werks == werks)


@lru_cache(maxsize=1)
def links() -> tuple[dict[str, list[Link]], dict[str, list[Link]]]:
    """(header -> item links, component -> links of the BOMs using it)."""
    down: dict[str, list[Link]] = defaultdict(list)
    up: dict[str, list[Link]] = defaultdict(list)
    for m in load_materials().values():
        headers = {(h["STLNR"], h["STLAL"]): h for h in m.stko}
        for item in sorted(m.stpo, key=lambda r: (r["STLAN"], r["STLAL"], r["POSNR"])):
            header = headers.get((item["STLNR"], item["STLAL"]), {})
            child = strip_matnr(item["IDNRK"]) if item.get("IDNRK") else None
            link = Link(parent=m.id, child=child, stlan=item["STLAN"], stlal=item["STLAL"],
                        werks=header.get("WRKAN", ""), base=f"{number(header.get('BMENG', ''))} {header.get('BMEIN', '')}".strip(),
                        posnr=item["POSNR"], postp=item["POSTP"], menge=number(item["MENGE"]) if item["MENGE"] else "",
                        meins=item["MEINS"], text=" ".join(t for t in (item.get("POTX1"), item.get("POTX2")) if t))
            down[m.id].append(link)
            if child:
                up[child].append(link)
    return down, up


def node(mid: Optional[str], link: Link, **extra) -> dict:
    m = load_materials().get(mid) if mid else None
    return {
        "matnr": mid or "", "text": m.short_text if m else link.text or ITEM_CATEGORIES.get(link.postp, ""),
        "posnr": link.posnr, "postp": link.postp, "postp_text": ITEM_CATEGORIES.get(link.postp, link.postp),
        "menge": link.menge, "meins": link.meins, "bom": link.bom, "base": link.base,
        "is_bom": bool(m and m.stko), "deleted": bool(m and m.deleted), **extra,
    }


def select_bom(mid: str, stlan: str, werks: str, parent: Link) -> list[Link]:
    """Items of the one BOM of a component that is exploded below `parent` (like CS12): same plant (or plant
    independent), preferably the same usage, lowest alternative."""
    down, _ = links()
    candidates = [l for l in down.get(mid, []) if l.matches(stlan, werks) and l.werks in (parent.werks, "")]
    if not candidates:
        return []
    best = min({(l.stlan, l.stlal, l.werks) for l in candidates},
               key=lambda k: (k[0] != parent.stlan, k[2] != parent.werks, k[0], k[1]))
    return [l for l in candidates if (l.stlan, l.stlal, l.werks) == best]


def explode(mid: str, stlan: str = "", werks: str = "", path: tuple = (), parent: Optional[Link] = None) -> list[dict]:
    """Multi-level BOM explosion: all matching BOMs of the material, below that one BOM per component."""
    down, _ = links()
    path = path + (mid,)
    items = select_bom(mid, stlan, werks, parent) if parent else [l for l in down.get(mid, []) if l.matches(stlan, werks)]
    result = []
    for link in items:
        cycle = link.child in path
        children = [] if cycle or not link.child or len(path) >= MAX_DEPTH else explode(link.child, stlan, werks, path, link)
        result.append(node(link.child, link, level=len(path), cycle=cycle, children=children))
    return result


def where_used(mid: str, stlan: str = "", werks: str = "", path: tuple = ()) -> list[dict]:
    """Multi-level where-used list (like CS15): the BOM headers using the material, up to the top level."""
    _, up = links()
    path = path + (mid,)
    result = []
    for link in up.get(mid, []):
        if not link.matches(stlan, werks):
            continue
        cycle = link.parent in path
        parents = [] if cycle or len(path) >= MAX_DEPTH else where_used(link.parent, stlan, werks, path)
        # quantity / position are those of the usage of `mid` in this BOM
        result.append(node(link.parent, link, level=len(path), cycle=cycle, children=parents))
    return result


def graph(mid: str, stlan: str = "", werks: str = "") -> dict:
    """Nodes and lines of all BOMs above (where-used) and below (explosion) the material."""
    down, up = links()
    materials = load_materials()
    levels = {mid: 0}
    edges: dict[tuple[str, str], list[Link]] = defaultdict(list)
    truncated = False
    for direction in ("up", "down"):
        queue = [mid]
        while queue:
            current = queue.pop(0)
            for link in (up if direction == "up" else down).get(current, []):
                if not link.child or not link.matches(stlan, werks) or link.parent == link.child:
                    continue
                other = link.parent if direction == "up" else link.child
                edges[(link.parent, link.child)].append(link)
                if other not in levels:
                    if len(levels) >= MAX_GRAPH_NODES:
                        truncated = True
                        continue
                    levels[other] = levels[current] + (-1 if direction == "up" else 1)
                    queue.append(other)
    nodes = []
    for key, level in levels.items():
        m = materials.get(key)
        nodes.append({"key": key, "title": m.short_text if m else key, "matnr": key, "level": level,
                      "role": "current" if key == mid else "parent" if level < 0 else "child",
                      "is_bom": bool(m and m.stko), "deleted": bool(m and m.deleted)})
    lines = [{"from": p, "to": c, "title": "; ".join(dict.fromkeys(f"{l.menge} {l.meins}".strip() for l in ls)),
              "description": " | ".join(dict.fromkeys(l.bom for l in ls))}
             for (p, c), ls in edges.items() if p in levels and c in levels]
    widest = max(Counter(levels.values()).values(), default=1)
    return {"nodes": nodes, "lines": lines, "truncated": truncated, "widest": widest}


def options(mid: str) -> dict:
    """Usages and plants that occur in the BOMs connected to the material (for the filter)."""
    down, up = links()
    seen, queue, found = {mid}, [mid], []
    while queue:
        current = queue.pop()
        for link in down.get(current, []) + up.get(current, []):
            found.append(link)
            for other in (link.parent, link.child):
                if other and other not in seen:
                    seen.add(other)
                    queue.append(other)
    usages = sorted({l.stlan for l in found})
    plants = sorted({l.werks for l in found if l.werks})
    return {"stlan": [{"key": u, "text": f"{u} {USAGES.get(u, '')}".strip()} for u in usages],
            "werks": [{"key": w, "text": w} for w in plants]}


def structure(mid: str, stlan: str = "", werks: str = "") -> dict:
    m = load_materials()[mid]
    headers = [{"stlan": h["STLAN"], "usage": USAGES.get(h["STLAN"], ""), "stlal": h["STLAL"], "stlnr": h["STLNR"],
                "werks": h.get("WRKAN", ""), "base": f"{number(h['BMENG'])} {h['BMEIN']}" if h.get("BMENG") else "",
                "valid_from": h.get("DATUV", ""), "status": h.get("STLST", ""),
                "items": sum(1 for i in m.stpo if (i["STLNR"], i["STLAL"]) == (h["STLNR"], h["STLAL"]))}
               for h in m.stko
               if (not stlan or h["STLAN"] == stlan) and (not werks or h.get("WRKAN", "") == werks)]
    down, _ = links()
    return {
        "matnr": mid,
        "headers": headers,
        "headers_total": len(m.stko),
        "explosion": explode(mid, stlan, werks),
        "where_used": where_used(mid, stlan, werks),
        "graph": graph(mid, stlan, werks),
        "options": options(mid),
        "self_reference": any(l.child == mid for l in down.get(mid, [])),
        "has_relations": bool(m.stko or m.bom_parents),
    }
