"""Shared Gemini client and structured-output helper.

Both the resume parser and the email classifier go through this module so
there is one place that owns the API key, the model name, and error mapping.
"""

import json
import os
import sys
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import GEMINI_API_KEY, GEMINI_MODEL, logger

_client: Optional[genai.Client] = None


def get_client() -> genai.Client:
    """Lazily build the Gemini client.

    Built on first use rather than at import time, so a missing API key
    surfaces as a handled error in the UI instead of crashing startup.
    """
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def handle_api_exception(exception_obj: Exception, context_tag: str) -> dict:
    """Translate an SDK exception into an error dict the UI can render."""
    error_message = str(exception_obj)
    logger.error("Gemini failure during [%s]: %s", context_tag, error_message)

    if "429" in error_message or "RESOURCE_EXHAUSTED" in error_message:
        return {
            "error": "RATE_LIMIT",
            "message": "API quota exceeded. Please try again in a minute.",
        }
    if "API_KEY" in error_message.upper() or "PERMISSION_DENIED" in error_message:
        return {
            "error": "AUTH_ERROR",
            "message": "Gemini rejected the API key. Check GEMINI_API_KEY in .env.",
        }
    if "GEMINI_API_KEY is not set" in error_message:
        return {"error": "CONFIG_ERROR", "message": error_message}

    return {
        "error": "GENERAL_ERROR",
        "message": "Processing failed. Check logs/app.log for details.",
    }


def generate_structured(prompt: str, schema: type[BaseModel], context_tag: str) -> dict:
    """Run a structured-output call, returning parsed JSON or an error dict."""
    try:
        response = get_client().models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        return json.loads(response.text or "{}")
    except Exception as exc:  # noqa: BLE001 - surfaced to callers as an error dict
        return handle_api_exception(exc, context_tag)
