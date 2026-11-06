"""Read access to SAP OData services (live data, read only): client, logon language, basic auth, paging."""
import os
from typing import Iterator

import requests

from .config import REQUEST_TIMEOUT, SAP_CLIENT, SAP_LOGON_LANGUAGE, SAP_ODATA_URL

PAGE = 5000


class SapError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


def configured() -> bool:
    return bool(os.getenv("SAP_USER") and os.getenv("SAP_PASSWORD"))


def get(service: str, entity: str, params: dict) -> dict:
    """One request; returns the OData v2 'd' object. Raises SapError with the SAP message on HTTP errors."""
    try:
        response = requests.get(
            f"{SAP_ODATA_URL}/{service}/{entity}",
            params={"sap-client": SAP_CLIENT,
                    **({"sap-language": SAP_LOGON_LANGUAGE} if SAP_LOGON_LANGUAGE else {}),   # German codes (TA, AG)
                    "$format": "json", **params},
            headers={"Accept": "application/json",
                     **({"Accept-Language": SAP_LOGON_LANGUAGE.lower()} if SAP_LOGON_LANGUAGE else {})},
            auth=(os.environ["SAP_USER"], os.environ["SAP_PASSWORD"]),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise SapError(f"SAP nicht erreichbar: {e}") from e
    if not response.ok:
        try:
            message = response.json()["error"]["message"]["value"]
        except (ValueError, KeyError, TypeError):
            message = response.reason
        raise SapError(f"{service}: HTTP {response.status_code} {message}", response.status_code)
    return response.json()["d"]


def get_all(service: str, entity: str, params: dict) -> Iterator[dict]:
    """All rows of a query, page by page."""
    skip = 0
    while True:
        rows = get(service, entity, {**params, "$top": str(PAGE), "$skip": str(skip)})["results"]
        yield from rows
        if len(rows) < PAGE:
            return
        skip += PAGE


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def any_of(field: str, values: list[str]) -> str:
    return "(" + " or ".join(f"{field} eq {quote(v)}" for v in values) + ")"
