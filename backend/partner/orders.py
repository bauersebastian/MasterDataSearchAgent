"""Open sales orders (Kundenaufträge, VA01) of a customer, read live from SAP via the standard OData API
API_SALES_ORDER_SRV (A_SalesOrder). "Open" = not fully delivered (overall delivery status A / B) and not fully
rejected; header level only, the customer as sold-to party."""
import re
from datetime import datetime, timezone

from .. import odata
from ..tables import strip_zeros
from ..config import ORDERS_LIMIT, SAP_CLIENT, SAP_ODATA_URL

SapError = odata.SapError
DELIVERY_STATUS = {"A": "nicht geliefert", "B": "teilweise geliefert", "C": "vollständig geliefert"}
FIELDS = ("SalesOrder", "SalesOrderType", "SalesOrganization", "DistributionChannel", "OrganizationDivision",
          "CreationDate", "CreatedByUser", "PurchaseOrderByCustomer", "RequestedDeliveryDate", "TotalNetAmount",
          "TransactionCurrency", "OverallDeliveryStatus", "OverallSDProcessStatus", "DeliveryBlockReason",
          "HeaderBillingBlockReason", "TotalBlockStatus")


def odata_date(value: str | None) -> str:
    """'/Date(1759795200000)/' -> '2025-10-07'."""
    m = re.match(r"/Date\((-?\d+)", value or "")
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat() if m else ""


def open_orders(customer: str) -> dict:
    """Open sales orders where the customer (SAP-internal number, 10 digits) is the sold-to party, newest first."""
    data = odata.get("API_SALES_ORDER_SRV", "A_SalesOrder", {
        "$inlinecount": "allpages",
        "$top": str(ORDERS_LIMIT),
        "$orderby": "CreationDate desc,SalesOrder desc",
        "$filter": f"SoldToParty eq {odata.quote(customer)} and (OverallDeliveryStatus eq 'A' or "
                   f"OverallDeliveryStatus eq 'B') and OverallSDDocumentRejectionSts ne 'C'",
        "$select": ",".join(FIELDS),
    })

    orders = []
    totals: dict[str, float] = {}
    for r in data["results"]:
        amount = float(r.get("TotalNetAmount") or 0)
        currency = r.get("TransactionCurrency", "")
        totals[currency] = totals.get(currency, 0) + amount
        status = r.get("OverallDeliveryStatus", "")
        orders.append({
            "order": r["SalesOrder"],
            "type": r.get("SalesOrderType", ""),
            "sales_area": "/".join(x for x in (r.get("SalesOrganization"), r.get("DistributionChannel"),
                                               r.get("OrganizationDivision")) if x),
            "created": odata_date(r.get("CreationDate")),
            "created_by": r.get("CreatedByUser", ""),
            "customer_po": r.get("PurchaseOrderByCustomer", ""),
            "requested_delivery": odata_date(r.get("RequestedDeliveryDate")),
            "net_amount": amount,
            "currency": currency,
            "delivery_status": status,
            "delivery_status_text": DELIVERY_STATUS.get(status, status),
            "delivery_block": r.get("DeliveryBlockReason", ""),
            "billing_block": r.get("HeaderBillingBlockReason", ""),
        })
    count = int(data.get("__count") or len(orders))
    return {"customer": strip_zeros(customer), "orders": orders, "count": count, "truncated": count > len(orders),
            "totals": [{"currency": c, "amount": round(a, 2)} for c, a in sorted(totals.items())],
            "source": f"{SAP_ODATA_URL}/API_SALES_ORDER_SRV (Mandant {SAP_CLIENT})"}
