"""Read the SAP extract files: one ';' separated file per table, `<OBJ>_<timestamp>_<nr>_<TABLE>.txt`."""
import csv
from pathlib import Path


def table_file(folder: Path, table: str) -> Path:
    files = sorted(folder.glob(f"*_{table}.txt"))
    if not files:
        raise FileNotFoundError(f"Keine Datei *_{table}.txt in {folder}")
    return files[-1]   # newest extract


def read_table(folder: Path, table: str, optional: bool = False) -> list[dict]:
    if optional and not any(folder.glob(f"*_{table}.txt")):
        return []
    with open(table_file(folder, table), encoding="utf-8", newline="") as fh:
        rows = csv.DictReader(fh, delimiter=";", quoting=csv.QUOTE_NONE)
        return [{k: (v or "").strip() for k, v in row.items() if k} for row in rows]


def strip_zeros(number: str) -> str:
    """SAP internal (zero padded) number -> display form."""
    return (number.lstrip("0") or "0") if number.isdigit() else number


def pad(number: str, length: int) -> str:
    number = number.strip()
    return number.zfill(length) if number.isdigit() else number.upper()
