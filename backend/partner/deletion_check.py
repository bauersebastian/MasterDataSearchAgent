"""Check before deleting a business partner: open documents in SAP that still reference the partner, read live via
the standard OData APIs, on demand only. Areas whose API is not available are reported as "not checked" -- never
as passed -- so the check never says "no obstacles" just because an API is missing.

Checked (customer role):
- sales documents below: fully rejected ones (rejection status C) only as a hint, they are effectively closed even
  when the processing status is not C
- sales orders not completed (overall processing status != C), the customer in any partner function
  (sold-to, ship-to, bill-to, payer, ...), header and item partners
- sales quotations not completed, any partner function; expired quotations only as a warning
- billing documents not (yet) posted to accounting (transfer status not C / D / E, not cancelled), sold-to or payer
- open FI items of the customer account (API_OPLACCTGDOCITEMCUBE_SRV); noted items (down payment requests) only as a hint
- outbound deliveries without completed goods issue (goods movement status A / B), sold-to or ship-to
- contracts, scheduling agreements, returns, credit memo requests not completed, any partner function, header and
  item partners; contracts / scheduling agreements whose validity has ended only as a hint

Checked (supplier role) -- the supplier as supplier (LF), invoicing party (RS), goods supplier (WL), ordering address
(BA) or bidder (BI), wherever the API has the field or a partner table:
- open FI items of the supplier account, same service
- supplier invoices not posted (parked / held), API_SUPPLIERINVOICE_PROCESS_SRV; posted ones are in the FI items
- purchase orders not deleted with items not completely delivered (API_PURCHASEORDER_PROCESS_SRV); the final
  invoice flag is not maintained in the system, so "not finally invoiced" is no criterion
- purchasing scheduling agreements and contracts not deleted (API_SCHED_AGRMT_PROCESS_SRV,
  API_PURCHASECONTRACT_PROCESS_SRV;v=0002); expired ones only as a hint
- inbound deliveries without completed goods receipt (API_INBOUND_DELIVERY_SRV;v=0002)
- hints only: purchase requisitions not yet converted (supplier as fixed / desired source), open requests for quotation
  with the supplier as bidder (this also covers supplier quotations), purchasing info records from the extract (EINA)

Partner numbers: filters use the 10-digit SAP number; the services return numbers without leading zeros, so results
are compared via `same_number`.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from .. import odata
from ..tables import strip_zeros
from .data import Partner

log = logging.getLogger("mdsa")

CHUNK = 40   # document numbers per filter (URL length)

PARTNER_FUNCTIONS = {"AG": "Auftraggeber", "WE": "Warenempfänger", "RE": "Rechnungsempfänger", "RG": "Regulierer",
                     "AP": "Ansprechpartner", "VE": "Vertriebsmitarbeiter", "SP": "Spediteur", "ZM": "Zuständiger MA",
                     "LF": "Lieferant", "RS": "Rechnungssteller", "WL": "Warenlieferant", "BA": "Bestelladresse",
                     "BI": "Bieter"}
DELIVERY_STATUS = {"A": "nicht geliefert", "B": "teilweise geliefert", "C": "vollständig geliefert"}
PROCESS_STATUS = {"A": "offen", "B": "in Arbeit", "C": "erledigt", "": "ohne Status"}
TRANSFER_STATUS = {"": "Buchungsbeleg nicht erzeugt", "A": "für Übergabe an FI gesperrt",
                   "B": "nicht gebucht (Kontenfindungsfehler)", "F": "nicht gebucht (Preisfindungsfehler)",
                   "G": "nicht gebucht (Außenhandelsdaten fehlen)", "H": "nicht gebucht", "K": "nicht gebucht"}

# areas that would belong to a complete check but whose API is not available in the system yet
NOT_AVAILABLE_CUSTOMER: list[tuple[str, str, str]] = []
NOT_AVAILABLE_VENDOR: list[tuple[str, str, str]] = []


FI_SERVICE, FI_ENTITY = "API_OPLACCTGDOCITEMCUBE_SRV", "A_OperationalAcctgDocItemCube"
SPECIAL_GL = {"A": "Anzahlung", "F": "Anzahlungsanforderung", "D": "Rabattrückstellung", "W": "Wechsel"}


def rejected(row: dict) -> bool:
    """Sales document fully rejected (all items with a reason for rejection)."""
    return row.get("OverallSDDocumentRejectionSts") == "C"


def same_number(a: str, b: str) -> bool:
    """Partner numbers equal regardless of leading zeros ('1001591' == '0001001591')."""
    return strip_zeros(a or "") == strip_zeros(b or "")


def role_text(code: str) -> str:
    return f"{code} {PARTNER_FUNCTIONS[code]}" if code in PARTNER_FUNCTIONS else code


def chunks(values: list[str]):
    for i in range(0, len(values), CHUNK):
        yield values[i:i + CHUNK]


def partner_documents(service: str, entities: list[str], key: str, number: str, field: str = "Customer",
                      item_field: str = "") -> dict[str, set[str]]:
    """Document number -> partner functions in which the partner appears (header and item partners). Item partners
    are recognized by the entity name (sales APIs) or, with `item_field`, by a filled item number (purchasing)."""
    docs: dict[str, set[str]] = {}
    for entity in entities:
        for r in odata.get_all(service, entity, {"$filter": f"{field} eq {odata.quote(number)}",
                                                 "$select": ",".join(x for x in (key, "PartnerFunction", item_field) if x)}):
            item = r.get(item_field, "").strip("0") if item_field else "Item" in entity
            docs.setdefault(r[key], set()).add(r["PartnerFunction"] + (" (Position)" if item else ""))
    return docs


def supplier_fields(row: dict, supplier: str) -> set[str]:
    """Partner functions from the supplier fields of a purchasing document header."""
    return {code for code, field in (("LF", "Supplier"), ("RS", "InvoicingParty"), ("WL", "SupplyingSupplier"))
            if same_number(row.get(field, ""), supplier)}


def supplier_filter(supplier: str, fields: tuple[str, ...] = ("Supplier", "InvoicingParty", "SupplyingSupplier")) -> str:
    return "(" + " or ".join(f"{f} eq {odata.quote(supplier)}" for f in fields) + ")"


def headers(service: str, entity: str, key: str, numbers: list[str], condition: str, fields: str) -> list[dict]:
    rows = []
    for part in chunks(sorted(numbers)):
        rows += odata.get(service, entity, {"$filter": odata.any_of(key, part) + (f" and {condition}" if condition else ""),
                                            "$select": fields, "$top": str(CHUNK)})["results"]
    return rows


def check_sales_orders(customer: str) -> dict:
    docs = partner_documents("API_SALES_ORDER_SRV", ["A_SalesOrderHeaderPartner", "A_SalesOrderItemPartner"],
                             "SalesOrder", customer)
    rows = headers("API_SALES_ORDER_SRV", "A_SalesOrder", "SalesOrder", list(docs), "OverallSDProcessStatus ne 'C'",
                   "SalesOrder,SalesOrderType,CreationDate,PurchaseOrderByCustomer,TotalNetAmount,TransactionCurrency,"
                   "OverallSDProcessStatus,OverallDeliveryStatus,OverallSDDocumentRejectionSts,SoldToParty")
    documents = [{
        "number": r["SalesOrder"], "type": r["SalesOrderType"], "date": odata.date(r["CreationDate"]),
        "roles": sorted(role_text(f) for f in docs[r["SalesOrder"]]),
        "status": f"{PROCESS_STATUS.get(r['OverallSDProcessStatus'], r['OverallSDProcessStatus'])}"
                  + (f", {DELIVERY_STATUS[r['OverallDeliveryStatus']]}" if r["OverallDeliveryStatus"] in DELIVERY_STATUS else "")
                  + (", vollständig abgelehnt" if rejected(r) else ""),
        "amount": float(r["TotalNetAmount"] or 0), "currency": r["TransactionCurrency"],
        "info": f"Auftraggeber {strip_zeros(r['SoldToParty'])}" if not same_number(r["SoldToParty"], customer) else r["PurchaseOrderByCustomer"],
        "blocking": not rejected(r),
    } for r in rows]
    return result("sales_orders", "Kundenaufträge (nicht erledigt)", "API_SALES_ORDER_SRV", documents,
                  f"{len(docs)} Aufträge mit dem Kunden als Partner geprüft; vollständig abgelehnte nur als Hinweis")


def check_quotations(customer: str) -> dict:
    docs = partner_documents("API_SALES_QUOTATION_SRV", ["A_SalesQuotationPartner", "A_SalesQuotationItemPartner"],
                             "SalesQuotation", customer)
    rows = headers("API_SALES_QUOTATION_SRV", "A_SalesQuotation", "SalesQuotation", list(docs),
                   "OverallSDProcessStatus ne 'C'",
                   "SalesQuotation,SalesQuotationType,CreationDate,BindingPeriodValidityEndDate,TotalNetAmount,"
                   "TransactionCurrency,OverallSDProcessStatus,OverallSDDocumentRejectionSts,SoldToParty")
    today = date.today().isoformat()
    documents = []
    for r in rows:
        valid_to = odata.date(r["BindingPeriodValidityEndDate"])
        expired = bool(valid_to) and valid_to < today
        documents.append({
            "number": r["SalesQuotation"], "type": r["SalesQuotationType"], "date": odata.date(r["CreationDate"]),
            "roles": sorted(role_text(f) for f in docs[r["SalesQuotation"]]),
            "status": PROCESS_STATUS.get(r["OverallSDProcessStatus"], r["OverallSDProcessStatus"])
                      + (f", abgelaufen am {valid_to}" if expired else f", gültig bis {valid_to}" if valid_to else "")
                      + (", vollständig abgelehnt" if rejected(r) else ""),
            "amount": float(r["TotalNetAmount"] or 0), "currency": r["TransactionCurrency"],
            "info": f"Auftraggeber {strip_zeros(r['SoldToParty'])}" if not same_number(r["SoldToParty"], customer) else "",
            "blocking": not expired and not rejected(r),
        })
    return result("quotations", "Angebote (nicht erledigt)", "API_SALES_QUOTATION_SRV", documents,
                  f"{len(docs)} Angebote mit dem Kunden als Partner geprüft; abgelaufene und abgelehnte nur als Hinweis")


def check_billing(customer: str) -> dict:
    c = odata.quote(customer)
    rows = list(odata.get_all("API_BILLING_DOCUMENT_SRV", "A_BillingDocument", {
        "$filter": f"(SoldToParty eq {c} or PayerParty eq {c}) and AccountingTransferStatus ne 'C' and "
                   f"AccountingTransferStatus ne 'D' and AccountingTransferStatus ne 'E' and BillingDocumentIsCancelled eq false",
        "$select": "BillingDocument,BillingDocumentType,BillingDocumentDate,TotalNetAmount,TransactionCurrency,"
                   "AccountingTransferStatus,SoldToParty,PayerParty"}))
    documents = [{
        "number": r["BillingDocument"], "type": r["BillingDocumentType"], "date": odata.date(r["BillingDocumentDate"]),
        "roles": [role_text(f) for f, p in (("AG", r["SoldToParty"]), ("RG", r["PayerParty"])) if same_number(p, customer)],
        "status": TRANSFER_STATUS.get(r["AccountingTransferStatus"], f"Status {r['AccountingTransferStatus']}"),
        "amount": float(r["TotalNetAmount"] or 0), "currency": r["TransactionCurrency"], "info": "",
        "blocking": True,
    } for r in rows]
    return result("billing", "Fakturen nicht an die Buchhaltung übergeben", "API_BILLING_DOCUMENT_SRV", documents,
                  "Auftraggeber oder Regulierer, nicht storniert")


DELIVERY_SERVICE = "API_OUTBOUND_DELIVERY_SRV;v=0002"
GOODS_MOVEMENT_STATUS = {"A": "Warenausgang nicht gebucht", "B": "Warenausgang teilweise gebucht"}


def check_deliveries(customer: str) -> dict:
    docs = partner_documents(DELIVERY_SERVICE, ["A_OutbDeliveryPartner"], "SDDocument", customer)
    rows = headers(DELIVERY_SERVICE, "A_OutbDeliveryHeader", "DeliveryDocument", list(docs),
                   "(OverallGoodsMovementStatus eq 'A' or OverallGoodsMovementStatus eq 'B')",
                   "DeliveryDocument,DeliveryDocumentType,CreationDate,PlannedGoodsIssueDate,OverallGoodsMovementStatus,"
                   "SoldToParty,ShipToParty")
    documents = [{
        "number": r["DeliveryDocument"], "type": r["DeliveryDocumentType"], "date": odata.date(r["CreationDate"]),
        "roles": sorted(role_text(f) for f in docs[r["DeliveryDocument"]]),
        "status": GOODS_MOVEMENT_STATUS.get(r["OverallGoodsMovementStatus"], r["OverallGoodsMovementStatus"])
                  + (f", geplant {odata.date(r['PlannedGoodsIssueDate'])}" if r["PlannedGoodsIssueDate"] else ""),
        "amount": None, "currency": "",
        "info": " · ".join(x for x in (
            f"Auftraggeber {strip_zeros(r['SoldToParty'])}" if not same_number(r["SoldToParty"], customer) else "",
            f"Warenempfänger {strip_zeros(r['ShipToParty'])}" if not same_number(r["ShipToParty"], customer) else "") if x),
        "blocking": True,
    } for r in rows]
    return result("deliveries", "Lieferungen ohne Warenausgang", DELIVERY_SERVICE, documents,
                  f"{len(docs)} Lieferungen mit dem Kunden als Partner geprüft")


def sd_documents(check_id: str, title: str, service: str, header: str, key: str, partner_entities: list[str],
                 type_field: str, customer: str, valid_to_field: str = "") -> dict:
    """Sales documents not completed (overall processing status != C) with the customer in any partner function;
    fully rejected documents and, with `valid_to_field`, documents whose validity has ended are only a hint."""
    docs = partner_documents(service, partner_entities, key, customer)
    rows = headers(service, header, key, list(docs), "OverallSDProcessStatus ne 'C'",
                   ",".join(x for x in (key, type_field, "CreationDate", valid_to_field, "TotalNetAmount",
                                        "TransactionCurrency", "OverallSDProcessStatus", "OverallSDDocumentRejectionSts",
                                        "SoldToParty") if x))
    today = date.today().isoformat()
    documents = []
    for r in rows:
        valid_to = odata.date(r[valid_to_field]) if valid_to_field else ""
        expired = bool(valid_to) and valid_to < today
        documents.append({
            "number": r[key], "type": r[type_field], "date": odata.date(r["CreationDate"]),
            "roles": sorted(role_text(f) for f in docs[r[key]]),
            "status": PROCESS_STATUS.get(r["OverallSDProcessStatus"], r["OverallSDProcessStatus"])
                      + (f", abgelaufen am {valid_to}" if expired else f", gültig bis {valid_to}" if valid_to else "")
                      + (", vollständig abgelehnt" if rejected(r) else ""),
            "amount": float(r["TotalNetAmount"] or 0), "currency": r["TransactionCurrency"],
            "info": f"Auftraggeber {strip_zeros(r['SoldToParty'])}" if not same_number(r["SoldToParty"], customer) else "",
            "blocking": not expired and not rejected(r),
        })
    return result(check_id, title, service, documents,
                  f"{len(docs)} Belege mit dem Kunden als Partner geprüft; "
                  + ("abgelaufene und abgelehnte" if valid_to_field else "vollständig abgelehnte") + " nur als Hinweis")


def check_contracts(customer: str) -> dict:
    return sd_documents("contracts", "Kontrakte (nicht erledigt)", "API_SALES_CONTRACT_SRV", "A_SalesContract",
                        "SalesContract", ["A_SalesContractPartner", "A_SalesContractItemPartner"], "SalesContractType",
                        customer, "SalesContractValidityEndDate")


def check_scheduling_agreements(customer: str) -> dict:
    return sd_documents("scheduling_agreements", "Lieferpläne (nicht erledigt)", "API_SALES_SCHEDULING_AGREEMENT",
                        "A_SalesSchedgAgrmt", "SalesSchedulingAgreement",
                        ["A_SalesSchedgAgrmtPartner", "A_SalesSchedgAgrmtItemPartner"], "SalesSchedgAgrmtType",
                        customer, "AgrmtValdtyEndDate")


def check_returns(customer: str) -> dict:
    return sd_documents("returns", "Retouren (nicht erledigt)", "API_CUSTOMER_RETURN_SRV", "A_CustomerReturn",
                        "CustomerReturn", ["A_CustomerReturnPartner", "A_CustomerReturnItemPartner"],
                        "CustomerReturnType", customer)


def check_credit_memo_requests(customer: str) -> dict:
    return sd_documents("credit_memo_requests", "Gutschriftsanforderungen (nicht erledigt)",
                        "API_CREDIT_MEMO_REQUEST_SRV", "A_CreditMemoRequest", "CreditMemoRequest",
                        ["A_CreditMemoReqPartner", "A_CreditMemoReqItemPartner"], "CreditMemoRequestType", customer)


# --- supplier role ------------------------------------------------------------------------------------------------
INVOICE_SERVICE = "API_SUPPLIERINVOICE_PROCESS_SRV"
PO_SERVICE = "API_PURCHASEORDER_PROCESS_SRV"
SA_SERVICE = "API_SCHED_AGRMT_PROCESS_SRV"
CONTRACT_SERVICE = "API_PURCHASECONTRACT_PROCESS_SRV;v=0002"
INBOUND_SERVICE = "API_INBOUND_DELIVERY_SRV;v=0002"
PR_SERVICE = "API_PURCHASEREQ_PROCESS_SRV"
RFQ_SERVICE = "API_RFQ_PROCESS_SRV"
INVOICE_STATUS = {"A": "vorerfasst", "B": "vorerfasst, als vollständig gekennzeichnet", "C": "vorerfasst und freigegeben",
                  "D": "erfasst und angehalten", "E": "vorerfasst und freigegeben"}
GOODS_RECEIPT_STATUS = {"A": "Wareneingang nicht gebucht", "B": "Wareneingang teilweise gebucht"}
RFQ_STATUS = {"01": "Entwurf", "02": "veröffentlicht"}


def check_supplier_invoices(supplier: str) -> dict:
    rows = odata.get_all(INVOICE_SERVICE, "A_SupplierInvoice", {
        "$filter": f"InvoicingParty eq {odata.quote(supplier)} and SupplierInvoiceStatus ne '5'",
        "$select": "SupplierInvoice,FiscalYear,DocumentDate,InvoiceGrossAmount,DocumentCurrency,SupplierInvoiceStatus,"
                   "SupplierInvoiceIsCreditMemo,SupplierInvoiceIDByInvcgParty,PaymentBlockingReason"})
    documents = [{
        "number": f"{r['SupplierInvoice']}/{r['FiscalYear']}",
        "type": "Gutschrift" if r["SupplierInvoiceIsCreditMemo"] == "X" else "Rechnung",
        "date": odata.date(r["DocumentDate"]), "roles": ["RS Rechnungssteller"],
        "status": INVOICE_STATUS.get(r["SupplierInvoiceStatus"], f"Status {r['SupplierInvoiceStatus']}") + ", nicht gebucht",
        "amount": float(r["InvoiceGrossAmount"] or 0), "currency": r["DocumentCurrency"],
        "info": " · ".join(x for x in (f"Referenz {r['SupplierInvoiceIDByInvcgParty']}" if r["SupplierInvoiceIDByInvcgParty"] else "",
                                       f"Zahlsperre {r['PaymentBlockingReason']}" if r["PaymentBlockingReason"] else "") if x),
        "blocking": True,
    } for r in rows]
    return result("supplier_invoices", "Eingangsrechnungen nicht gebucht", INVOICE_SERVICE, documents,
                  "vorerfasste oder angehaltene Rechnungen und Gutschriften des Lieferanten als Rechnungssteller; "
                  "gebuchte stehen in den offenen Posten")


def check_purchase_orders(supplier: str) -> dict:
    heads = {r["PurchaseOrder"]: r for r in odata.get_all(PO_SERVICE, "A_PurchaseOrder", {
        "$filter": f"{supplier_filter(supplier)} and PurchasingDocumentDeletionCode eq ''",
        "$select": "PurchaseOrder,PurchaseOrderType,PurchaseOrderDate,CreationDate,DocumentCurrency,Supplier,"
                   "InvoicingParty,SupplyingSupplier,ReleaseIsNotCompleted"})}
    items: dict[str, list[dict]] = {}
    for part in chunks(sorted(heads)):
        for r in odata.get_all(PO_SERVICE, "A_PurchaseOrderItem", {
                "$filter": f"{odata.any_of('PurchaseOrder', part)} and PurchasingDocumentDeletionCode eq ''",
                "$select": "PurchaseOrder,PurchaseOrderItem,IsCompletelyDelivered,OrderQuantity,NetPriceAmount,NetPriceQuantity"}):
            items.setdefault(r["PurchaseOrder"], []).append(r)
    documents = []
    for po, its in items.items():
        open_items = [i for i in its if not i["IsCompletelyDelivered"]]
        if not open_items:
            continue
        h = heads[po]
        documents.append({
            "number": po, "type": h["PurchaseOrderType"], "date": odata.date(h["PurchaseOrderDate"] or h["CreationDate"]),
            "roles": sorted(role_text(c) for c in supplier_fields(h, supplier)),
            "status": f"{len(open_items)} von {len(its)} Positionen nicht vollständig geliefert"
                      + (", Freigabe offen" if h["ReleaseIsNotCompleted"] else ""),
            "amount": round(sum(float(i["OrderQuantity"] or 0) * float(i["NetPriceAmount"] or 0)
                                / (float(i["NetPriceQuantity"] or 0) or 1) for i in open_items), 2),
            "currency": h["DocumentCurrency"], "info": "Wert der offenen Positionen (Bestellmenge × Nettopreis)",
            "blocking": True,
        })
    return result("purchase_orders", "Bestellungen nicht vollständig geliefert", PO_SERVICE, documents,
                  f"{len(heads)} nicht gelöschte Bestellungen mit dem Lieferanten als Lieferant, Rechnungssteller oder "
                  "Warenlieferant geprüft")


def outline_agreements(check_id: str, title: str, service: str, header: str, key: str, partner_entity: str,
                       item_field: str, type_field: str, amount_field: str, supplier: str) -> dict:
    """Purchasing scheduling agreements / contracts not deleted; the supplier in the header fields or the partner
    table; expired ones (validity end in the past) only as a hint."""
    docs = partner_documents(service, [partner_entity], key, supplier, "Supplier", item_field)
    for r in odata.get_all(service, header, {"$filter": supplier_filter(supplier),
                                             "$select": f"{key},Supplier,InvoicingParty,SupplyingSupplier"}):
        docs.setdefault(r[key], set()).update(supplier_fields(r, supplier))
    # the purchasing partner table (EKPA) also holds purchase orders: only numbers found as header count
    rows = headers(service, header, key, list(docs), "",
                   f"{key},{type_field},CreationDate,ValidityEndDate,{amount_field},DocumentCurrency,"
                   "PurchasingDocumentDeletionCode")
    today = date.today().isoformat()
    documents = []
    for r in rows:
        if r["PurchasingDocumentDeletionCode"]:
            continue
        valid_to = odata.date(r["ValidityEndDate"])
        expired = bool(valid_to) and valid_to < today
        amount = float(r[amount_field] or 0)
        documents.append({
            "number": r[key], "type": r[type_field], "date": odata.date(r["CreationDate"]),
            "roles": sorted(role_text(f) for f in docs[r[key]]),
            "status": f"abgelaufen am {valid_to}" if expired else f"gültig bis {valid_to}" if valid_to else "ohne Gültigkeitsende",
            "amount": amount or None, "currency": r["DocumentCurrency"] if amount else "",
            "info": "Zielwert" if amount else "", "blocking": not expired,
        })
    return result(check_id, title, service, documents,
                  f"{len(rows)} Belege mit dem Lieferanten als Partner geprüft; gelöschte nicht, abgelaufene nur als Hinweis")


def check_purchasing_scheduling_agreements(supplier: str) -> dict:
    return outline_agreements("purchasing_scheduling_agreements", "Lieferpläne Einkauf", SA_SERVICE, "A_SchAgrmtHeader",
                              "SchedulingAgreement", "A_SchAgrmtPartner", "SchedulingAgreementItem",
                              "PurchasingDocumentType", "TargetAmount", supplier)


def check_purchase_contracts(supplier: str) -> dict:
    return outline_agreements("purchase_contracts", "Kontrakte Einkauf", CONTRACT_SERVICE, "A_PurchaseContract",
                              "PurchaseContract", "A_PurCtrPartners", "PurchaseContractItem", "PurchaseContractType",
                              "PurchaseContractTargetAmount", supplier)


def check_inbound_deliveries(supplier: str) -> dict:
    docs = partner_documents(INBOUND_SERVICE, ["A_InbDeliveryPartner"], "SDDocument", supplier, "Supplier", "SDDocumentItem")
    for r in odata.get_all(INBOUND_SERVICE, "A_InbDeliveryHeader", {"$filter": f"Supplier eq {odata.quote(supplier)}",
                                                                    "$select": "DeliveryDocument"}):
        docs.setdefault(r["DeliveryDocument"], set()).add("LF")
    rows = headers(INBOUND_SERVICE, "A_InbDeliveryHeader", "DeliveryDocument", list(docs),
                   "(OverallGoodsMovementStatus eq 'A' or OverallGoodsMovementStatus eq 'B')",
                   "DeliveryDocument,DeliveryDocumentType,CreationDate,DeliveryDate,OverallGoodsMovementStatus,"
                   "DeliveryDocumentBySupplier")
    documents = [{
        "number": r["DeliveryDocument"], "type": r["DeliveryDocumentType"], "date": odata.date(r["CreationDate"]),
        "roles": sorted(role_text(f) for f in docs[r["DeliveryDocument"]]),
        "status": GOODS_RECEIPT_STATUS.get(r["OverallGoodsMovementStatus"], r["OverallGoodsMovementStatus"])
                  + (f", Liefertermin {odata.date(r['DeliveryDate'])}" if r["DeliveryDate"] else ""),
        "amount": None, "currency": "",
        "info": f"Lieferschein {r['DeliveryDocumentBySupplier']}" if r["DeliveryDocumentBySupplier"] else "",
        "blocking": True,
    } for r in rows]
    return result("inbound_deliveries", "Anlieferungen ohne Wareneingang", INBOUND_SERVICE, documents,
                  f"{len(docs)} Anlieferungen mit dem Lieferanten geprüft")


def check_purchase_requisitions(supplier: str) -> dict:
    s = odata.quote(supplier)
    rows = odata.get_all(PR_SERVICE, "A_PurchaseRequisitionItem", {
        "$filter": f"(Supplier eq {s} or FixedSupplier eq {s}) and IsDeleted eq '' and IsClosed eq false "
                   "and ProcessingStatus eq 'N'",
        "$select": "PurchaseRequisition,PurchaseRequisitionItem,PurchaseRequisitionType,CreationDate,DeliveryDate,"
                   "ItemNetAmount,PurReqnItemCurrency,Material,PurchaseRequisitionItemText,Supplier,FixedSupplier"})
    documents = [{
        "number": f"{r['PurchaseRequisition']}/{r['PurchaseRequisitionItem'].lstrip('0')}",
        "type": r["PurchaseRequisitionType"], "date": odata.date(r["CreationDate"]),
        "roles": ["fester Lieferant" if same_number(r["FixedSupplier"], supplier) else "gewünschter Lieferant"],
        "status": "nicht bearbeitet" + (f", Liefertermin {odata.date(r['DeliveryDate'])}" if r["DeliveryDate"] else ""),
        "amount": float(r["ItemNetAmount"] or 0) or None, "currency": r["PurReqnItemCurrency"],
        "info": " ".join(x for x in (r["Material"].lstrip("0"), r["PurchaseRequisitionItemText"]) if x),
        "blocking": False,
    } for r in rows]
    return result("purchase_requisitions", "Bestellanforderungen (Hinweis)", PR_SERVICE, documents,
                  "nicht gelöschte, noch nicht in Bestellungen umgesetzte Positionen mit dem Lieferanten als festem "
                  "oder gewünschtem Lieferanten; nur als Hinweis")


def check_rfqs(supplier: str) -> dict:
    docs = partner_documents(RFQ_SERVICE, ["A_RequestForQuotationBidder"], "RequestForQuotation", supplier, "Supplier")
    rows = headers(RFQ_SERVICE, "A_RequestForQuotation", "RequestForQuotation", list(docs),
                   "(RFQLifecycleStatus eq '01' or RFQLifecycleStatus eq '02')",
                   "RequestForQuotation,PurchasingDocumentType,CreationDate,RFQLifecycleStatus,QuotationLatestSubmissionDate")
    documents = [{
        "number": r["RequestForQuotation"], "type": r["PurchasingDocumentType"], "date": odata.date(r["CreationDate"]),
        "roles": sorted(role_text(f) for f in docs[r["RequestForQuotation"]]),
        "status": RFQ_STATUS.get(r["RFQLifecycleStatus"], r["RFQLifecycleStatus"])
                  + (f", Angebotsfrist {odata.date(r['QuotationLatestSubmissionDate'])}" if r["QuotationLatestSubmissionDate"] else ""),
        "amount": None, "currency": "", "info": "", "blocking": False,
    } for r in rows]
    return result("rfqs", "Anfragen (Hinweis)", RFQ_SERVICE, documents,
                  f"{len(docs)} Anfragen mit dem Lieferanten als Bieter geprüft (deckt auch Lieferantenangebote ab); "
                  "Entwurf oder veröffentlicht, nur als Hinweis")


def info_records(partner: Partner) -> dict:
    """Purchasing info records of the supplier from the extract (EINA) -- master data, not documents: only a hint."""
    documents = [{
        "number": m["infnr"].lstrip("0") or "-", "type": "Infosatz", "date": "", "roles": [],
        "status": "vorhanden (Stand letzter Datenabzug)", "amount": None, "currency": "",
        "info": " · ".join(x for x in (f"Material {m['matnr']}", m["text"], f"Lieferantenmaterial {m['idnlf']}" if m["idnlf"] else "") if x),
        "blocking": False,
    } for m in partner.materials if not m["deleted"]]
    return result("info_records", "Einkaufsinfosätze (Hinweis)", "Extrakt EINA", documents,
                  "nicht gelöschte Einkaufsinfosätze laut Materialdaten der Suche (Stand des letzten Datenabzugs, "
                  "nicht live aus SAP); nur als Hinweis")


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
        due = odata.date(r["NetDueDate"])
        noted = r["AccountingDocumentCategory"] == "S"   # noted item: not posted to the balance
        special = r["SpecialGLCode"]
        documents.append({
            "number": f"{r['AccountingDocument']}/{r['FiscalYear']}", "type": r["AccountingDocumentType"],
            "date": odata.date(r["PostingDate"]),
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
    hints = len(documents) - blocking
    summary = ", ".join(x for x in (f"{blocking} offen" if blocking else "", f"{hints} Hinweis(e)" if hints else "")
                        if x) or "keine offenen Belege"
    return {"id": check_id, "title": title, "service": service, "status": status, "summary": summary,
            "scope": scope, "documents": documents}


def run_check(fn, check_id: str, title: str, service: str, number: str) -> dict:
    try:
        return fn(number)
    except odata.SapError as e:
        # technical details (service, HTTP status, SAP message) only in the log and the "error" field, not in the dialog
        log.warning("Löschprüfung %s (%s) für %s: %s", check_id, service, number, e)
        status = "not_checked" if e.status_code in (401, 403, 404) else "error"
        return {"id": check_id, "title": title, "service": service, "status": status, "documents": [], "error": str(e),
                "summary": "nicht geprüft" if status == "not_checked" else "Fehler bei der Prüfung",
                "scope": "Dieser Bereich ist in SAP nicht freigeschaltet oder es fehlt die Berechtigung – bitte manuell "
                         "prüfen." if status == "not_checked" else
                         "Die Daten konnten nicht aus SAP gelesen werden – bitte erneut prüfen oder manuell prüfen."}


def not_available(items: list[tuple[str, str, str]]) -> list[dict]:
    return [{"id": i, "title": t, "service": s, "status": "not_checked", "documents": [],
             "summary": "nicht geprüft",
             "scope": "Dieser Bereich ist in SAP nicht freigeschaltet – bitte manuell prüfen."} for i, t, s in items]


def check(partner: Partner) -> dict:
    runs = []
    if partner.kunnr:
        runs += [(check_sales_orders, "sales_orders", "Kundenaufträge (nicht erledigt)", "API_SALES_ORDER_SRV", partner.kunnr_sap),
                 (check_quotations, "quotations", "Angebote (nicht erledigt)", "API_SALES_QUOTATION_SRV", partner.kunnr_sap),
                 (check_billing, "billing", "Fakturen nicht an die Buchhaltung übergeben", "API_BILLING_DOCUMENT_SRV",
                  partner.kunnr_sap),
                 (check_receivables, "receivables", "Offene Posten Debitor (FI)", FI_SERVICE, partner.kunnr_sap),
                 (check_deliveries, "deliveries", "Lieferungen ohne Warenausgang", DELIVERY_SERVICE, partner.kunnr_sap),
                 (check_contracts, "contracts", "Kontrakte (nicht erledigt)", "API_SALES_CONTRACT_SRV", partner.kunnr_sap),
                 (check_scheduling_agreements, "scheduling_agreements", "Lieferpläne (nicht erledigt)",
                  "API_SALES_SCHEDULING_AGREEMENT", partner.kunnr_sap),
                 (check_returns, "returns", "Retouren (nicht erledigt)", "API_CUSTOMER_RETURN_SRV", partner.kunnr_sap),
                 (check_credit_memo_requests, "credit_memo_requests", "Gutschriftsanforderungen (nicht erledigt)",
                  "API_CREDIT_MEMO_REQUEST_SRV", partner.kunnr_sap)]
    if partner.lifnr:
        n = partner.lifnr_sap
        runs += [(check_payables, "payables", "Offene Posten Kreditor (FI)", FI_SERVICE, n),
                 (check_supplier_invoices, "supplier_invoices", "Eingangsrechnungen nicht gebucht", INVOICE_SERVICE, n),
                 (check_purchase_orders, "purchase_orders", "Bestellungen nicht vollständig geliefert", PO_SERVICE, n),
                 (check_purchasing_scheduling_agreements, "purchasing_scheduling_agreements", "Lieferpläne Einkauf",
                  SA_SERVICE, n),
                 (check_purchase_contracts, "purchase_contracts", "Kontrakte Einkauf", CONTRACT_SERVICE, n),
                 (check_inbound_deliveries, "inbound_deliveries", "Anlieferungen ohne Wareneingang", INBOUND_SERVICE, n),
                 (check_purchase_requisitions, "purchase_requisitions", "Bestellanforderungen (Hinweis)", PR_SERVICE, n),
                 (check_rfqs, "rfqs", "Anfragen (Hinweis)", RFQ_SERVICE, n)]
    checks: list[dict] = []
    if runs:
        with ThreadPoolExecutor(len(runs)) as pool:
            checks += list(pool.map(lambda r: run_check(*r), runs))
    if partner.kunnr:
        checks += not_available(NOT_AVAILABLE_CUSTOMER)
    if partner.lifnr:
        checks += not_available(NOT_AVAILABLE_VENDOR)
        checks.append(info_records(partner))

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
