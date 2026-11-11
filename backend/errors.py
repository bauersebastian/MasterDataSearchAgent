"""HTTP errors shared by the API routers."""
import logging

import openai
from fastapi import HTTPException

log = logging.getLogger("mdsa")


def openai_error(e: openai.OpenAIError) -> HTTPException:
    log.exception("OpenAI request failed")
    return HTTPException(502, f"OpenAI-Aufruf fehlgeschlagen: {e}")
