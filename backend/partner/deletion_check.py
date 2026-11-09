"""Check before deleting a business partner: open documents in SAP that still reference the partner, read live via
the standard OData APIs, on demand only. Areas whose API is not available are reported as "not checked" -- never
as passed -- so the check never says "no obstacles" just because an API is missing.

Checked (customer role):
- sales orders not completed (overall processing status != C), the customer in any partner function
  (sold-to, ship-to, bill-to, payer, ...), header and item partners
- sales quotations not completed, any partner function; expired quotations only as a warning
- billing documents not (yet) posted to accounting (transfer status not C / D / E, not cancelled), sold-to or payer
- open FI items of the customer account (API_OPLACCTGDOCITEMCUBE_SRV); noted items (down payment requests) only as a hint

Checked (supplier role):
- open FI items of the supplier account, same service

Partner numbers: filters use the 10-digit SAP number; the services return numbers without leading zeros, so results
are compared via `same_number`.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from .. import odata
from ..tables import strip_zeros
from .data import Partner
from .orders import DELIVERY_STATUS, odata_date

CHUNK = 40   # document numbers per filter (URL length)

PARTNER_FUNCTIONS = {"AG": "Auftraggeber", "WE": "Warenempfänger", "RE": "Rechnungsempfänger", "RG": "Regulierer",
                     "AP": "Ansprechpartner", "VE": "Vertriebsmitarbeiter", "SP": "Spediteur", "ZM": "Zuständiger MA"}
PROCESS_STATUS = {"A": "offen", "B": "in Arbeit", "C": "erledigt", "": "ohne Status"}
TRANSFER_STATUS = {"": "Buchungsbeleg nicht erzeugt", "A": "für Übergabe an FI gesperrt",
                   "B": "nicht gebucht (Kontenfindungsfehler)", "F": "nicht gebucht (Preisfindungsfehler)",
                   "G": "nicht gebucht (Außenhandelsdaten fehlen)", "H": "nicht gebucht", "K": "nicht gebucht"}

# areas that would belong to a complete check but whose API is not available in the system yet
NOT_AVAILABLE_CUSTOMER = [
    ("deliveries", "Lieferungen ohne Warenausgang", "API_OUTBOUND_DELIVERY_SRV;v=0002"),
    ("contracts", "Kontrakte", "API_SALES_CONTRACT_SRV"),
    ("scheduling_agreements", "Lieferpläne", "API_SALES_SCHEDULING_AGREEMENT"),
    ("returns", "Retouren", "API_CUSTOMER_RETURN_SRV"),
    ("credit_memo_requests", "Gutschriftsanforderungen", "API_CREDIT_MEMO_REQUEST_SRV"),
]
NOT_AVAILABLE_VENDOR = [
    ("purchase_orders", "Offene Bestellungen", "API_PURCHASEORDER_PROCESS_SRV"),
]


FI_SERVICE, FI_ENTITY = "API_OPLACCTGDOCITEMCUBE_SRV", "A_OperationalAcctgDocItemCube"
SPECIAL_GL = {"A": "Anzahlung", "F": "Anzahlungsanforderung", "D": "Rabattrückstellung", "W": "Wechsel"}


def same_number(a: str, b: str) -> bool:
    """Partner numbers equal regardless of leading zeros ('1001591' == '0001001591')."""
    return strip_zeros(a or "") == strip_zeros(b or "")


def role_text(code: str) -> str:
    return f"{code} {PARTNER_FUNCTIONS[code]}" if code in PARTNER_FUNCTIONS else code


def chunks(values: list[str]):
    for i in range(0, len(values), CHUNK):
        yield values[i:i + CHUNK]


def partner_documents(service: str, entities: list[str], key: str, customer: str) -> dict[str, set[str]]:
    """Document number -> partner functions in which the customer appears (header and item partners)."""
    docs: dict[str, set[str]] = {}
    for entity in entities:
        for r in odata.get_all(service, entity, {"$filter": f"Customer eq {odata.quote(customer)}",
                                                 "$select": f"{key},PartnerFunction"}):
            docs.setdefault(r[key], set()).add(r["PartnerFunction"] + (" (Position)" if "Item" in entity else ""))
    return docs


def headers(service: str, entity: str, key: str, numbers: list[str], condition: str, fields: str) -> list[dict]:
    rows = []
    for part in chunks(sorted(numbers)):
        rows += odata.get(service, entity, {"$filter": f"{odata.any_of(key, part)} and {condition}",
                                            "$select": fields, "$top": str(CHUNK)})["results"]
    return rows


def check_sales_orders(customer: str) -> dict:
    docs = partner_documents("API_SALES_ORDER_SRV", ["A_SalesOrderHeaderPartner", "A_SalesOrderItemPartner"],
                             "SalesOrder", customer)
    rows = headers("API_SALES_ORDER_SRV", "A_SalesOrder", "SalesOrder", list(docs), "OverallSDProcessStatus ne 'C'",
                   "SalesOrder,SalesOrderType,CreationDate,PurchaseOrderByCustomer,TotalNetAmount,TransactionCurrency,"
                   "OverallSDProcessStatus,OverallDeliveryStatus,SoldToParty")
    documents = [{
        "number": r["SalesOrder"], "type": r["SalesOrderType"], "date": odata_date(r["CreationDate"]),
        "roles": sorted(role_text(f) for f in docs[r["SalesOrder"]]),
        "status": f"{PROCESS_STATUS.get(r['OverallSDProcessStatus'], r['OverallSDProcessStatus'])}"
                  + (f", {DELIVERY_STATUS[r['OverallDeliveryStatus']]}" if r["OverallDeliveryStatus"] in DELIVERY_STATUS else ""),
        "amount": float(r["TotalNetAmount"] or 0), "currency": r["TransactionCurrency"],
        "info": f"Auftraggeber {strip_zeros(r['SoldToParty'])}" if not same_number(r["SoldToParty"], customer) else r["PurchaseOrderByCustomer"],
        "blocking": True,
    } for r in rows]
    return result("sales_orders", "Kundenaufträge (nicht erledigt)", "API_SALES_ORDER_SRV", documents,
                  f"{len(docs)} Aufträge mit dem Kunden als Partner geprüft")


def check_quotations(customer: str) -> dict:
    docs = partner_documents("API_SALES_QUOTATION_SRV", ["A_SalesQuotationPartner", "A_SalesQuotationItemPartner"],
                             "SalesQuotation", customer)
    rows = headers("API_SALES_QUOTATION_SRV", "A_SalesQuotation", "SalesQuotation", list(docs),
                   "OverallSDProcessStatus ne 'C'",
                   "SalesQuotation,SalesQuotationType,CreationDate,BindingPeriodValidityEndDate,TotalNetAmount,"
                   "TransactionCurrency,OverallSDProcessStatus,SoldToParty")
    today = date.today().isoformat()
    documents = []
    for r in rows:
        valid_to = odata_date(r["BindingPeriodValidityEndDate"])
        expired = bool(valid_to) and valid_to < today
        documents.append({
            "number": r["SalesQuotation"], "type": r["SalesQuotationType"], "date": odata_date(r["CreationDate"]),
            "roles": sorted(role_text(f) for f in docs[r["SalesQuotation"]]),
            "status": PROCESS_STATUS.get(r["OverallSDProcessStatus"], r["OverallSDProcessStatus"])
                      + (f", abgelaufen am {valid_to}" if expired else f", gültig bis {valid_to}" if valid_to else ""),
            "amount": float(r["TotalNetAmount"] or 0), "currency": r["TransactionCurrency"],
            "info": f"Auftraggeber {strip_zeros(r['SoldToParty'])}" if not same_number(r["SoldToParty"], customer) else "",
            "blocking": not expired,
        })
    return result("quotations", "Angebote (nicht erledigt)", "API_SALES_QUOTATION_SRV", documents,
                  f"{len(docs)} Angebote mit dem Kunden als Partner geprüft; abgelaufene nur als Hinweis")


def check_billing(customer: str) -> dict:
    c = odata.quote(customer)
    rows = list(odata.get_all("API_BILLING_DOCUMENT_SRV", "A_BillingDocument", {
        "$filter": f"(SoldToParty eq {c} or PayerParty eq {c}) and AccountingTransferStatus ne 'C' and "
                   f"AccountingTransferStatus ne 'D' and AccountingTransferStatus ne 'E' and BillingDocumentIsCancelled eq false",
        "$select": "BillingDocument,BillingDocumentType,BillingDocumentDate,TotalNetAmount,TransactionCurrency,"
                   "AccountingTransferStatus,SoldToParty,PayerParty"}))
    documents = [{
        "number": r["BillingDocument"], "type": r["BillingDocumentType"], "date": odata_date(r["BillingDocumentDate"]),
        "roles": [role_text(f) for f, p in (("AG", r["SoldToParty"]), ("RG", r["PayerParty"])) if same_number(p, customer)],
        "status": TRANSFER_STATUS.get(r["AccountingTransferStatus"], f"Status {r['AccountingTransferStatus']}"),
        "amount": float(r["TotalNetAmount"] or 0), "currency": r["TransactionCurrency"], "info": "",
        "blocking": True,
    } for r in rows]
    return result("billing", "Fakturen nicht an die Buchhaltung übergeben", "API_BILLING_DOCUMENT_SRV", documents,
                  "Auftraggeber oder Regulierer, nicht storniert")


def open_items(field: str, account_type: str, number: str) -> list[dict]:
    """Open (not cleared) FI items of a customer (field Customer, type D) or supplier (Supplier, K) account."""
    rows = odata.get_all(FI_SERVICE, FI_ENTITY, {
        "$filter": f"{field} eq {odata.quote(number)} and FinancialAccountType eq '{account_type}' and IsCleared eq false",
        "$select": "CompanyCode,AccountingDocument,FiscalYear,AccountingDocumentItem,AccountingDocumentType,PostingDate,"
                   "NetDueDate,AmountInCompanyCodeCurrency,CompanyCodeCurrency,SpecialGLCode,AccountingDocumentCategory,"
                   "DocumentItemText,AssignmentReference"})
    today = date.today().isoformat()
    documents = []
    for r in rows:
        due = odata_date(r["NetDueDate"])
        noted = r["AccountingDocumentCategory"] == "S"   # noted item: not posted to the balance
        special = r["SpecialGLCode"]
        documents.append({
            "number": f"{r['AccountingDocument']}/{r['FiscalYear']}", "type": r["AccountingDocumentType"],
            "date": odata_date(r["PostingDate"]),
            "roles": [f"Sonderhauptbuch {special}" + (f" {SPECIAL_GL[special]}" if special in SPECIAL_GL else "")] if special else [],
            "status": ("Merkposten, " if noted else "offen, ")
                      + (f"fällig seit {due}" if due and due < today else f"fällig am {due}" if due else "ohne Fälligkeit"),
            "amount": float(r["AmountInCompanyCodeCurrency"] or 0), "currency": r["CompanyCodeCurrency"],
            "info": " · ".join(x for x in (f"Buchungskreis {r['CompanyCode']}", r["AssignmentReference"].lstrip("0"),
                                           r["DocumentItemText"]) if x),
            "blocking": not noted,
        })
    return documents


def balance(documents: list[dict]) -> str:
    totals: dict[str, float] = {}
    for d in documents:
        if d["blocking"]:
            totals[d["currency"]] = totals.get(d["currency"], 0) + d["amount"]
    return ", ".join(f"{a:,.2f} {c}".replace(",", "X").replace(".", ",").replace("X", ".") for c, a in sorted(totals.items()))


def check_receivables(customer: str) -> dict:
    documents = open_items("Customer", "D", customer)
    saldo = balance(documents)
    return result("receivables", "Offene Posten Debitor (FI)", FI_SERVICE, documents,
                  "nicht ausgeglichene Posten des Debitorenkontos, alle Buchungskreise; Merkposten nur als Hinweis"
                  + (f"; Saldo offene Posten {saldo}" if saldo else ""))


def check_payables(supplier: str) -> dict:
    documents = open_items("Supplier", "K", supplier)
    saldo = balance(documents)
    return result("payables", "Offene Posten Kreditor (FI)", FI_SERVICE, documents,
                  "nicht ausgeglichene Posten des Kreditorenkontos, alle Buchungskreise; Merkposten nur als Hinweis"
                  + (f"; Saldo offene Posten {saldo}" if saldo else ""))


def result(check_id: str, title: str, service: str, documents: list[dict], scope: str) -> dict:
    documents.sort(key=lambda d: d["date"], reverse=True)   # newest first, blocking documents before hints
    documents.sort(key=lambda d: not d["blocking"])
    blocking = sum(d["blocking"] for d in documents)
    status = "blocked" if blocking else "warning" if documents else "ok"
    summary = (f"{blocking} offen" + (f", {len(documents) - blocking} Hinweis(e)" if len(documents) > blocking else "")
               if documents else "keine offenen Belege")
    return {"id": check_id, "title": title, "service": service, "status": status, "summary": summary,
            "scope": scope, "documents": documents}


def run_check(fn, check_id: str, title: str, service: str, number: str) -> dict:
    try:
        return fn(number)
    except odata.SapError as e:
        status = "not_checked" if e.status_code in (401, 403, 404) else "error"
        return {"id": check_id, "title": title, "service": service, "status": status, "documents": [],
                "summary": "nicht geprüft – Service nicht verfügbar" if status == "not_checked" else "Fehler bei der Prüfung",
                "scope": str(e)}


def not_available(items: list[tuple[str, str, str]]) -> list[dict]:
    return [{"id": i, "title": t, "service": s, "status": "not_checked", "documents": [],
             "summary": "nicht geprüft – API im System nicht freigeschaltet",
             "scope": f"{s} ist nicht aktiv oder der Benutzer hat keine Berechtigung"} for i, t, s in items]


def check(partner: Partner) -> dict:
    runs = []
    if partner.kunnr:
        runs += [(check_sales_orders, "sales_orders", "Kundenaufträge (nicht erledigt)", "API_SALES_ORDER_SRV", partner.kunnr_sap),
                 (check_quotations, "quotations", "Angebote (nicht erledigt)", "API_SALES_QUOTATION_SRV", partner.kunnr_sap),
                 (check_billing, "billing", "Fakturen nicht an die Buchhaltung übergeben", "API_BILLING_DOCUMENT_SRV",
                  partner.kunnr_sap),
                 (check_receivables, "receivables", "Offene Posten Debitor (FI)", FI_SERVICE, partner.kunnr_sap)]
    if partner.lifnr:
        runs += [(check_payables, "payables", "Offene Posten Kreditor (FI)", FI_SERVICE, partner.lifnr_sap)]
    checks: list[dict] = []
    if runs:
        with ThreadPoolExecutor(len(runs)) as pool:
            checks += list(pool.map(lambda r: run_check(*r), runs))
    if partner.kunnr:
        checks += not_available(NOT_AVAILABLE_CUSTOMER)
    if partner.lifnr:
        checks += not_available(NOT_AVAILABLE_VENDOR)

    blocked = [c for c in checks if c["status"] == "blocked"]
    unchecked = [c for c in checks if c["status"] in ("not_checked", "error")]
    if not partner.kunnr and not partner.lifnr:
        verdict, text = "ok", "Der Partner hat keine Kunden- oder Lieferantenrolle – keine Belegprüfung erforderlich."
    elif blocked:
        verdict = "blocked"
        text = (f"Löschen nicht möglich: offene Belege in {len(blocked)} Bereich(en) "
                f"({', '.join(c['title'] + ': ' + c['summary'] for c in blocked)}).")
    elif unchecked:
        verdict = "incomplete"
        text = (f"In den geprüften Bereichen keine offenen Belege – {len(unchecked)} Bereich(e) konnten nicht geprüft "
                f"werden. Vor dem Löschen manuell prüfen.")
    else:
        verdict, text = "ok", "Keine offenen Belege gefunden."
    return {"partner": partner.id, "name": partner.display_name, "kunnr": partner.kunnr, "lifnr": partner.lifnr,
            "verdict": verdict, "text": text, "checks": checks,
            "checked_at": datetime.now().isoformat(timespec="seconds")}
