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
from google.genai import types as _gt  # noqa: E402

MODEL     = "gemini-3-flash-preview"
MODEL_PRO = "gemini-3-pro-preview"


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


def enrich_destination_info(
    destination: str,
    description: str = "",
    maps_url: str = "",
    additional_info: str = "",
) -> str:
    """
    Return an enrichment block (bullet points) to append to a destination's
    'Additional info' field.  Synchronous — wrap in st.spinner() on the call site.
    """
    parts = [
        "You are a concise travel guide. Enrich the following destination card with "
        "useful, practical info the traveller would want to know BEFORE arriving. "
        "Focus on: top things to do/see, hidden gems, what to search online, "
        "practical tips (opening hours, tickets, transport). "
        "Use short markdown bullet points (5-8 bullets max). "
        "Do NOT repeat info already in 'Existing notes'.",
        "",
        f"Destination: {destination}",
    ]
    if description:
        parts.append(f"Context: {description}")
    if maps_url:
        parts.append(f"Maps: {maps_url}")
    if additional_info:
        parts.append(f"Existing notes: {additional_info}")

    prompt = "\n".join(parts)
    try:
        resp = _client().models.generate_content(model=MODEL, contents=prompt)
        return (resp.text or "").strip()
    except Exception as exc:
        return f"❌ Enrichment failed: {exc}"


def generate_structured(
    model:           str,
    prompt:          str,
    response_schema: dict | typing.Any,
    use_search:      bool = False,
) -> dict:
    """Call Gemini with JSON-mode (structured output) and return the parsed dict.

    - `response_schema` is a Gemini JSON schema (dict or types.Schema). When
      `use_search=True`, response_schema is IGNORED (the Search tool is mutually
      exclusive with response_schema in the API), and JSON is requested via the
      mime-type only — the prompt itself must instruct the model to return JSON.
    - On any error, returns ``{"_error": "<message>", "summary": "", "findings": []}``
      so callers can persist a failure without crashing.
    """
    try:
        client = _client()

        if use_search:
            cfg_kwargs = dict(
                response_mime_type="application/json",
                tools=[_gt.Tool(google_search=_gt.GoogleSearch())],
            )
        else:
            cfg_kwargs = dict(
                response_mime_type="application/json",
                response_schema=response_schema,
            )

        config = _gt.GenerateContentConfig(**cfg_kwargs)
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=config,
        )

        text = (resp.text or "").strip()
        if not text:
            return {"_error": "empty response", "summary": "", "findings": []}

        import json as _json
        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            # When Search grounding is on the model sometimes wraps JSON in fences
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```", 2)[1]
                if cleaned.lower().startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.strip().rstrip("`").strip()
            try:
                return _json.loads(cleaned)
            except Exception as exc:
                return {"_error": f"invalid JSON: {exc}", "summary": "", "findings": []}
    except Exception as exc:
        return {"_error": str(exc), "summary": "", "findings": []}


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
