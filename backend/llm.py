"""Structured LLM calls (Responses API with a pydantic output schema)."""
from . import vectordb
from .config import MODEL, REASONING_EFFORT


def parse(system: str, user: str, schema):
    response = vectordb.client().responses.parse(
        model=MODEL,
        reasoning={"effort": REASONING_EFFORT},
        input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        text_format=schema,
    )
    if response.output_parsed is None:
        raise RuntimeError(f"Das LLM hat kein strukturiertes Ergebnis geliefert: {response.output_text!r}")
    return response.output_parsed
