"""SOAP message for the FIS DXTO import web service and posting to SAP -- message format taken over from the
MasterDataResearchAgent. The material is sent as it is stored in the extract: all tables, all filled fields."""
import os
import re
import xml.etree.ElementTree as ET

import requests

from .config import (ENDPOINT_URL, JOB_NAME, KEY_FIELD, OPERATION, REQUEST_TIMEOUT, SAP_LOGON_LANGUAGE, SEPARATOR,
                     SOAP_ACTION, VARIANT, VIA_JOB)
from .data import Material

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SAP_NS = "urn:sap-com:document:sap:soap:functions:mc-style"
ET.register_namespace("soap-env", SOAP_NS)
ET.register_namespace("n1", SAP_NS)   # "ns<digit>" prefixes are reserved by ElementTree

# extract columns that are not sent: the material number travels in KEY_FIELD, XDELE only marks deleted rows
SKIP_FIELDS = {"MATNR", "XDELE"}


def xml_tables(material: Material) -> dict[str, list[str]]:
    """Tables that are sent, with their field list (key field + fields filled in at least one row, extract order)."""
    tables = {}
    for table, rows in material.tables().items():
        if rows:
            fields = [f for f in rows[0] if f not in SKIP_FIELDS | {KEY_FIELD} and any(r.get(f) for r in rows)]
            tables[table] = [KEY_FIELD] + fields
    return tables


def to_line(row: dict, fields: list[str]) -> str:
    """'^' separated line; like the SAP example, every line ends with the separator."""
    def clean(value) -> str:
        if value is None:
            return ""
        return re.sub(r"[\r\n\t]+", " ", str(value)).replace(SEPARATOR, " ")
    return SEPARATOR.join(clean(row.get(f)) for f in fields) + SEPARATOR


def build_xml(material: Material) -> str:
    envelope = ET.Element(f"{{{SOAP_NS}}}Envelope")
    ET.SubElement(envelope, f"{{{SOAP_NS}}}Header")
    body = ET.SubElement(envelope, f"{{{SOAP_NS}}}Body")
    call = ET.SubElement(body, f"{{{SAP_NS}}}{OPERATION}")
    for tag, value in (("IfSep", SEPARATOR), ("IfVariant", VARIANT), ("IfViaJob", VIA_JOB), ("IfJobname", JOB_NAME)):
        ET.SubElement(call, tag).text = value or None

    tables = material.tables()
    it_dat = ET.SubElement(call, "ItDat")
    for table, fields in xml_tables(material).items():
        item = ET.SubElement(it_dat, "item")
        ET.SubElement(ET.SubElement(item, "TObj"), "item").text = table
        t_str = ET.SubElement(item, "TStr")
        ET.SubElement(t_str, "item").text = SEPARATOR.join(fields) + SEPARATOR
        for row in tables[table]:
            ET.SubElement(t_str, "item").text = to_line({**row, KEY_FIELD: material.matnr}, fields)

    ET.indent(envelope, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(envelope, encoding="unicode")


def message(material: Material) -> dict:
    tables = material.tables()
    return {
        "matnr": material.id,
        "xml": build_xml(material),
        "tables": [{"table": t, "rows": len(tables[t]), "fields": len(f) - 1} for t, f in xml_tables(material).items()],
        "file": f"{material.id}.xml",
        "deleted": material.deleted,
        "endpoint": ENDPOINT_URL,
    }


def post_xml(xml: str) -> dict:
    """Post the SOAP message; returns status and response body instead of raising on SAP errors."""
    response = requests.post(
        ENDPOINT_URL,
        params={"sap-language": SAP_LOGON_LANGUAGE} if SAP_LOGON_LANGUAGE else None,
        data=xml.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8",
                 **({"SOAPAction": SOAP_ACTION} if SOAP_ACTION else {}),
                 **({"Accept-Language": SAP_LOGON_LANGUAGE.lower()} if SAP_LOGON_LANGUAGE else {})},
        auth=(os.environ["SAP_USER"], os.environ["SAP_PASSWORD"]),
        timeout=REQUEST_TIMEOUT,
    )
    fault = "Fault>" in response.text
    return {
        "ok": response.ok and not fault,
        "status_code": response.status_code,
        "reason": response.reason,
        "fault": fault,
        "body": response.text,
    }
