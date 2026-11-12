"""Token consumption of the OpenAI calls: every LLM and embeddings call is logged to a small SQLite database and summed
up for the usage page (/usage.html). Independent of the search logic -- the only touch point is `record()` after a
call (llm.parse, vectordb.embed; the embeddings of the indexer are included).
"""
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from .config import USAGE_DB, USAGE_TIMEZONE

log = logging.getLogger("mdsa.usage")
router = APIRouter()
TIMEZONE = ZoneInfo(USAGE_TIMEZONE)
EMBEDDING = "embedding"

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id               INTEGER PRIMARY KEY,
    ts               TEXT NOT NULL,       -- UTC, ISO 8601
    kind             TEXT NOT NULL,       -- feature, e.g. partner-duplicates; 'embedding' for embeddings calls
    model            TEXT,
    input_tokens     INTEGER NOT NULL,
    output_tokens    INTEGER NOT NULL,
    total_tokens     INTEGER NOT NULL,
    cached_tokens    INTEGER NOT NULL,    -- part of input_tokens served from the prompt cache
    reasoning_tokens INTEGER NOT NULL     -- part of output_tokens used for reasoning
);
CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls (ts);
"""
SUMS = ("COUNT(*) AS calls, COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, COALESCE(SUM(total_tokens), 0) AS total_tokens, "
        "COALESCE(SUM(cached_tokens), 0) AS cached_tokens, COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens, "
        f"COALESCE(SUM(CASE WHEN kind = '{EMBEDDING}' THEN input_tokens END), 0) AS embedding_tokens")


def connect() -> sqlite3.Connection:
    USAGE_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(USAGE_DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def record(kind: str, response) -> None:
    """Log the token usage of a Responses API or embeddings response. Never raises: tracking must not break a call."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            log.warning("OpenAI response without usage information (%s)", kind)
            return
        # Responses API: input_tokens / output_tokens; embeddings: prompt_tokens only
        input_tokens = getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", None) or 0
        output_tokens = getattr(usage, "output_tokens", None) or 0
        row = (
            utc(datetime.now(timezone.utc)), kind, getattr(response, "model", None), input_tokens, output_tokens,
            getattr(usage, "total_tokens", None) or input_tokens + output_tokens,
            getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0,
            getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0,
        )
        with closing(connect()) as con, con:
            con.execute("INSERT INTO llm_calls (ts, kind, model, input_tokens, output_tokens, total_tokens, "
                        "cached_tokens, reasoning_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
    except Exception:
        log.exception("Could not record the token usage")


def summary() -> dict:
    now = datetime.now(TIMEZONE)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # date arithmetic first, then the time zone: midnight stays midnight across a daylight saving change
    week = datetime.combine(today.date() - timedelta(days=today.weekday()), datetime.min.time(), TIMEZONE)
    month = today.replace(day=1)
    with closing(connect()) as con:
        # last LLM call; the embeddings of every search query would otherwise always be the last call
        last = con.execute("SELECT * FROM llm_calls WHERE kind != ? ORDER BY id DESC LIMIT 1", (EMBEDDING,)).fetchone()
        periods = {name: dict(con.execute(f"SELECT {SUMS} FROM llm_calls WHERE ts >= ?", (utc(start),)).fetchone())
                   | {"since": start.isoformat(timespec="seconds")}
                   for name, start in (("today", today), ("week", week), ("month", month))}
    return {"timezone": str(TIMEZONE), "last": dict(last) if last else None, **periods}


@router.get("/api/usage")
def get_usage() -> dict:
    return summary()
