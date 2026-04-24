"""
Gemini AI helper — travel Q&A for each destination.
Uses google-genai SDK (google.genai).
"""

import sys
import os
import typing
import streamlit as st
from utils.config import cfg

# Ensure the venv's site-packages are first so google.genai is found
# even when the system has conflicting google namespace packages.
_venv = os.environ.get("VIRTUAL_ENV", "")
if _venv:
    _sp = os.path.join(_venv, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages")
    if _sp not in sys.path:
        sys.path.insert(0, _sp)

import google.genai as genai  # noqa: E402

MODEL = "gemini-3-flash-preview"


@st.cache_resource
def _client() -> genai.Client:
    """Return a single shared Gemini client for the lifetime of the app process.

    Creating a new Client on every call causes 'client has been closed' errors
    inside st.fragment reruns, because the SDK's internal HTTP session is torn
    down before the request completes. Caching with st.cache_resource keeps one
    long-lived client that is safe to reuse across reruns and fragments.
    """
    return genai.Client(api_key=cfg.gemini_api_key)


def _build_prompt(
    destination: str,
    question: str | None,
    context: str | None,
) -> str:
    parts = [
        "You are an expert travel guide. Be helpful, concise, and mobile-friendly "
        "(use short paragraphs and markdown bullet points where appropriate).\n",
        f"Destination: {destination}",
    ]
    if context:
        parts.append(f"Trip context: {context}")
    if question:
        parts.append(f"Question: {question}")
    else:
        parts.append(
            "Give me a quick overview: top 3 must-see spots, a local food tip, "
            "one practical travel tip, and estimated daily budget in USD."
        )
    return "\n".join(parts)


def stream_destination_response(
    destination: str,
    question: str | None = None,
    context: str | None = None,
) -> typing.Generator[str, None, None]:
    """Yield Gemini response text chunks for use with st.write_stream()."""
    prompt = _build_prompt(destination, question, context)
    try:
        for chunk in _client().models.generate_content_stream(
            model=MODEL,
            contents=prompt,
        ):
            if chunk.text:
                yield chunk.text
    except Exception as exc:
        yield f"\n\n⚠️ Gemini error: {exc}"
