"""
Nano Banana image generation + Supabase Storage helpers.

image_url field lifecycle:
  ""           → no illustration requested / deleted
  "generating" → background thread running
  "failed:..." → generation failed (message after the colon)
  "https://..."→ real public URL in Supabase Storage
"""

from __future__ import annotations

import uuid
import threading
import traceback
from datetime import datetime
from typing import Optional

import streamlit as st

from utils.gemini_helper import _client as _gemini_client
from utils.sheets import _sb

# ─── Config ───────────────────────────────────────────────────────────────────

_IMAGE_MODEL = "gemini-3.1-flash-image-preview"
BUCKET = "destination-images"

_STYLE_PREFIX = (
    "Create a small modern sticker-style illustration on a pure WHITE background. "
    "Color palette: modern, fresh, and vibrant — use soft pastels combined with "
    "bright accent colors (think coral, sky blue, mint, warm yellow, lavender). "
    "Composition rule: ONE single dominant subject placed prominently in the "
    "center-foreground that immediately represents the prompt; any background "
    "elements must be subtle, simple, and clearly secondary. "
    "The image must read clearly at 110×110 px — bold clean shapes, no clutter. "
    "Style: flat vector with gentle ink outlines, modern and friendly. "
    "No text, no labels, no borders, no frames. Square format. Subject: "
)

STATUS_GENERATING = "generating"


def is_real_url(image_url: str) -> bool:
    return image_url.startswith("http")


def is_generating(image_url: str) -> bool:
    return image_url == STATUS_GENERATING


def is_failed(image_url: str) -> bool:
    return image_url.startswith("failed:")


# ─── Internals ────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _set_status(entry_id: str, value: str) -> None:
    """Write image_url directly to Supabase (used from background threads too)."""
    try:
        _sb().table("itinerary").update({"image_url": value}).eq("entry_id", entry_id).execute()
    except Exception as exc:
        print(f"[images {_ts()}] _set_status failed: {exc}", flush=True)


def _extract_image_bytes(response) -> Optional[bytes]:
    """Extract the first non-thought image part from a Gemini response."""
    print(f"[images {_ts()}] Scanning response parts...", flush=True)
    try:
        parts = list(response.parts)
        print(f"[images {_ts()}]   total parts={len(parts)}", flush=True)
        for pi, part in enumerate(parts):
            is_thought = getattr(part, "thought", False)
            inline = getattr(part, "inline_data", None)
            text = getattr(part, "text", None)

            if is_thought:
                print(f"[images {_ts()}]   part[{pi}] → THOUGHT (skipped)", flush=True)
                continue

            if inline is not None:
                data = getattr(inline, "data", None)
                mime = getattr(inline, "mime_type", "?")
                print(f"[images {_ts()}]   part[{pi}] → inline_data "
                      f"mime={mime} bytes={len(data) if data else 'None'}", flush=True)
                if data:
                    return data
            elif text:
                print(f"[images {_ts()}]   part[{pi}] → text: {text[:80]!r}", flush=True)
            else:
                print(f"[images {_ts()}]   part[{pi}] → unknown part type", flush=True)

    except Exception as exc:
        print(f"[images {_ts()}] _extract_image_bytes error: {exc}", flush=True)
    print(f"[images {_ts()}]   → No image bytes found.", flush=True)
    return None


def _generate_bytes(user_prompt: str) -> bytes:
    full_prompt = f"{_STYLE_PREFIX}{user_prompt.strip()}"
    print(f"[images {_ts()}] Calling model={_IMAGE_MODEL}", flush=True)
    print(f"[images {_ts()}] Prompt: {full_prompt[:120]!r}", flush=True)

    client = _gemini_client()

    from google.genai import types as _gt
    config = _gt.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=_gt.ImageConfig(
            image_size="512",   # smallest available for 3.1 Flash (512×512)
            aspect_ratio="1:1",
        ),
    )
    print(f"[images {_ts()}] Config: response_modalities=['IMAGE'] image_size=512 aspect_ratio=1:1", flush=True)

    response = client.models.generate_content(
        model=_IMAGE_MODEL,
        contents=[full_prompt],  # must be a list per the API docs
        config=config,
    )
    print(f"[images {_ts()}] Gemini returned. candidates={len(response.candidates or [])}", flush=True)

    img_bytes = _extract_image_bytes(response)
    if not img_bytes:
        raise RuntimeError(
            "Nano Banana returned no image — check model access / billing tier."
        )
    print(f"[images {_ts()}] Got {len(img_bytes):,} image bytes.", flush=True)
    return img_bytes


def _upload_to_bucket(entry_id: str, img_bytes: bytes) -> str:
    filename = f"{entry_id}_{uuid.uuid4().hex[:6]}.png"
    print(f"[images {_ts()}] Uploading '{filename}' ({len(img_bytes):,} bytes) to '{BUCKET}'...", flush=True)
    sb = _sb()
    sb.storage.from_(BUCKET).upload(
        path=filename,
        file=img_bytes,
        file_options={"content-type": "image/png", "upsert": "true"},
    )
    url = sb.storage.from_(BUCKET).get_public_url(filename)
    print(f"[images {_ts()}] Upload done → {url}", flush=True)
    return url


def _path_from_url(url: str) -> Optional[str]:
    marker = f"/{BUCKET}/"
    if marker not in url:
        return None
    return url.split(marker, 1)[1].split("?", 1)[0]


def _cleanup_old_image(entry_id: str) -> None:
    """Delete the existing image for this entry from the bucket (if any)."""
    try:
        resp = _sb().table("itinerary").select("image_url").eq("entry_id", entry_id).execute()
        if resp.data:
            old_url = str(resp.data[0].get("image_url", "") or "")
            if is_real_url(old_url):
                print(f"[images {_ts()}] Deleting old image for {entry_id!r}...", flush=True)
                delete_image(old_url)
    except Exception as exc:
        print(f"[images {_ts()}] _cleanup_old_image error: {exc}", flush=True)


# ─── Public API ───────────────────────────────────────────────────────────────


def ensure_bucket_public() -> None:
    """
    Make sure the Storage bucket exists and has public read access.
    Call this once at app startup (non-fatal if it fails).
    """
    try:
        info = _sb().storage.get_bucket(BUCKET)
        is_public = getattr(info, "public", None)
        print(f"[images {_ts()}] bucket '{BUCKET}' public={is_public}", flush=True)
        if not is_public:
            print(f"[images {_ts()}] Setting bucket '{BUCKET}' to public...", flush=True)
            _sb().storage.update_bucket(BUCKET, options={"public": True})
            print(f"[images {_ts()}] Bucket is now public.", flush=True)
    except Exception as exc:
        print(f"[images {_ts()}] ensure_bucket_public error (non-fatal): {exc}", flush=True)


def delete_image(image_url: str) -> bool:
    """Remove an image file from Supabase bucket. Safe to call with any string."""
    if not is_real_url(image_url):
        return False
    path = _path_from_url(image_url)
    if not path:
        print(f"[images {_ts()}] delete_image: could not parse path from {image_url[:60]!r}", flush=True)
        return False
    try:
        _sb().storage.from_(BUCKET).remove([path])
        print(f"[images {_ts()}] delete_image: removed '{path}'", flush=True)
        return True
    except Exception as exc:
        print(f"[images {_ts()}] delete_image error: {exc}", flush=True)
        return False


def _generate_and_save_worker(entry_id: str, user_prompt: str) -> None:
    print(f"[images {_ts()}] [thread] START entry_id={entry_id!r}", flush=True)
    try:
        _cleanup_old_image(entry_id)
        img = _generate_bytes(user_prompt)
        url = _upload_to_bucket(entry_id, img)
        _set_status(entry_id, url)
        print(f"[images {_ts()}] [thread] ✅ DONE {entry_id!r}", flush=True)
    except Exception as exc:
        msg = f"failed: {exc}"
        print(f"[images {_ts()}] [thread] ❌ FAILED {entry_id!r}: {exc}", flush=True)
        print(traceback.format_exc(), flush=True)
        _set_status(entry_id, msg)


def trigger_async_generation(entry_id: str, user_prompt: str) -> None:
    """Set status to 'generating' immediately, then run in background thread."""
    if not user_prompt.strip():
        return
    print(f"[images {_ts()}] trigger_async_generation: spawning thread for {entry_id!r}", flush=True)
    _set_status(entry_id, STATUS_GENERATING)
    threading.Thread(
        target=_generate_and_save_worker,
        args=(entry_id, user_prompt),
        daemon=True,
    ).start()


def regenerate_sync(entry_id: str, user_prompt: str) -> Optional[str]:
    """Synchronous generation (used by edit form). Shows spinner in UI."""
    print(f"[images {_ts()}] regenerate_sync START {entry_id!r}", flush=True)
    _cleanup_old_image(entry_id)
    _set_status(entry_id, STATUS_GENERATING)
    try:
        img = _generate_bytes(user_prompt)
        url = _upload_to_bucket(entry_id, img)
        _set_status(entry_id, url)
        print(f"[images {_ts()}] regenerate_sync ✅ {entry_id!r}", flush=True)
        return url
    except Exception as exc:
        msg = f"failed: {exc}"
        _set_status(entry_id, msg)
        print(f"[images {_ts()}] regenerate_sync ❌ {exc}", flush=True)
        st.error(f"Image generation failed: {exc}")
        return None
