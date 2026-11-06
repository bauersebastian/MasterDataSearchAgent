"""SOAP message of a business partner for the FIS DXTO import web service -- same format as for materials:
one item per GPA table (TObj = table name, e.g. G000), header line with the SAP field names, one line per row.
The partner is sent as it is stored in the extract: all tables, all rows, all filled fields."""
from ..config import ENDPOINT_URL, PARTNER_KEY_FIELD, PARTNER_VARIANT
from ..material.sap import build_message
from .data import Partner

# XDELE marks deleted rows in the extract (they are not loaded); in G000 it is the archiving flag of the partner
# (BUT000-XDELE) and is sent like every other field
EXTRACT_ONLY = {"XDELE"}


def skipped(table: str) -> set[str]:
    return {PARTNER_KEY_FIELD} | (set() if table == "G000" else EXTRACT_ONLY)


def xml_tables(partner: Partner) -> dict[str, list[str]]:
    """Tables that are sent, with their field list (key field + fields filled in at least one row, extract order)."""
    tables = {}
    for table, rows in partner.tables().items():
        if rows:
            fields = [f for f in rows[0] if f not in skipped(table) and any(r.get(f) for r in rows)]
            tables[table] = [PARTNER_KEY_FIELD] + fields
    return tables


def build_xml(partner: Partner) -> str:
    """Every line carries the partner number as in the extract (10 digits) in the key field."""
    tables = partner.tables()
    number = partner.g000["PARTNER"]
    return build_message({table: (fields, [{**row, PARTNER_KEY_FIELD: row.get("PARTNER") or number}
                                           for row in tables[table]])
                          for table, fields in xml_tables(partner).items()}, PARTNER_VARIANT)


def message(partner: Partner) -> dict:
    tables = partner.tables()
    return {
        "object": "partner",
        "id": partner.id,
        "xml": build_xml(partner),
        "tables": [{"table": t, "rows": len(tables[t]), "fields": len(f) - 1} for t, f in xml_tables(partner).items()],
        "file": f"partner_{partner.id}.xml",
        "deleted": partner.deleted,
        "endpoint": ENDPOINT_URL,
        "variant": PARTNER_VARIANT,
    }
