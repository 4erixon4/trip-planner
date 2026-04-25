"""
Nano Banana image generation + Supabase Storage helpers.

image_url field lifecycle:
  ""           → no illustration requested / deleted
  "generating" → background thread running
  "failed:..." → generation failed (message after the colon)
  "https://..."→ real public URL in Supabase Storage
"""

from __future__ import annotations

import re
import uuid
import threading
import traceback
from typing import Optional

import streamlit as st

from utils.gemini_helper import _client as _gemini_client
from utils.sheets import _sb

# ─── Config ───────────────────────────────────────────────────────────────────

_IMAGE_MODEL = "gemini-3.1-flash-image-preview"
BUCKET = "destination-images"

_STYLE_PREFIX = (
    "Create a square icon-style illustration on a pure WHITE background. "
    "Rules: ONE single object or character centred and filling most of the frame — "
    "no scenes, no backgrounds, no secondary elements. "
    "Color palette: modern and vibrant, NOT pastel — use bold saturated hues "
    "(vivid blue, rich coral, deep green, warm amber, strong teal). "
    "Style: clean flat vector with crisp ink outlines, like a high-quality app icon. "
    "The subject must be immediately recognisable at 64×64 px. "
    "No text, no labels, no shadows, no gradients, no borders, no frames. "
    "Square format. Single subject: "
)

STATUS_GENERATING = "generating"


def is_real_url(image_url: str) -> bool:
    return image_url.startswith("http")


def is_generating(image_url: str) -> bool:
    return image_url == STATUS_GENERATING


def is_failed(image_url: str) -> bool:
    return image_url.startswith("failed:")


# ─── Internals ────────────────────────────────────────────────────────────────

def _set_status(entry_id: str, value: str) -> None:
    """Write image_url directly to Supabase (used from background threads too)."""
    try:
        _sb().table("itinerary").update({"image_url": value}).eq("entry_id", entry_id).execute()
    except Exception:
        pass


def _extract_image_bytes(response) -> Optional[bytes]:
    """Extract the first non-thought image part from a Gemini response."""
    try:
        for part in response.parts:
            if getattr(part, "thought", False):
                continue
            inline = getattr(part, "inline_data", None)
            if inline is not None:
                data = getattr(inline, "data", None)
                if data:
                    return data
    except Exception:
        pass
    return None


def _generate_bytes(user_prompt: str) -> bytes:
    full_prompt = f"{_STYLE_PREFIX}{user_prompt.strip()}"
    client = _gemini_client()

    from google.genai import types as _gt
    config = _gt.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=_gt.ImageConfig(
            image_size="512",
            aspect_ratio="1:1",
        ),
    )

    response = client.models.generate_content(
        model=_IMAGE_MODEL,
        contents=[full_prompt],
        config=config,
    )

    img_bytes = _extract_image_bytes(response)
    if not img_bytes:
        raise RuntimeError(
            "Nano Banana returned no image — check model access / billing tier."
        )
    return img_bytes


def _upload_to_bucket(entry_id: str, img_bytes: bytes) -> str:
    filename = f"{entry_id}_{uuid.uuid4().hex[:6]}.png"
    sb = _sb()
    sb.storage.from_(BUCKET).upload(
        path=filename,
        file=img_bytes,
        file_options={"content-type": "image/png", "upsert": "true"},
    )
    return sb.storage.from_(BUCKET).get_public_url(filename)


def _path_from_url(url: str) -> Optional[str]:
    marker = f"/{BUCKET}/"
    if marker not in url:
        return None
    return url.split(marker, 1)[1].split("?", 1)[0]


def _cleanup_old_image(entry_id: str) -> None:
    """Delete the existing bucket image for this entry (if any)."""
    try:
        resp = _sb().table("itinerary").select("image_url").eq("entry_id", entry_id).execute()
        if resp.data:
            old_url = str(resp.data[0].get("image_url", "") or "")
            if is_real_url(old_url):
                delete_image(old_url)
    except Exception:
        pass


# ─── Public API ───────────────────────────────────────────────────────────────


@st.cache_resource(show_spinner=False)
def ensure_bucket_public() -> None:
    """
    Make sure the Storage bucket has public read access.
    Cached with st.cache_resource so it runs once per server process, not per rerun.
    """
    try:
        info = _sb().storage.get_bucket(BUCKET)
        if not getattr(info, "public", False):
            _sb().storage.update_bucket(BUCKET, options={"public": True})
    except Exception:
        pass


# ─── Entry photo uploads ──────────────────────────────────────────────────────


def get_entry_images(entry_id: str) -> list[dict]:
    """Return list of {image_id, url, filename} for an entry."""
    try:
        resp = (
            _sb().table("entry_images")
            .select("image_id,url,filename")
            .eq("entry_id", entry_id)
            .order("created_at")
            .execute()
        )
        return resp.data or []
    except Exception:
        return []


def upload_entry_image(
    entry_id: str,
    trip_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str = "image/jpeg",
) -> str | None:
    """Upload a user photo, insert a row in entry_images, return public URL."""
    try:
        image_id = f"img_{uuid.uuid4().hex[:10]}"
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
        path = f"entry_images/{entry_id}/{image_id}_{safe_name}"
        _sb().storage.from_(BUCKET).upload(
            path,
            file_bytes,
            file_options={"content-type": content_type, "upsert": "true"},
        )
        pub = _sb().storage.from_(BUCKET).get_public_url(path)
        _sb().table("entry_images").insert({
            "image_id": image_id,
            "entry_id": entry_id,
            "trip_id": trip_id,
            "url": pub,
            "filename": filename,
            "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        }).execute()
        return pub
    except Exception as exc:
        import streamlit as _st
        _st.error(f"Upload failed: {exc}")
        return None


def delete_entry_image(image_id: str, image_url: str) -> bool:
    """Remove photo from bucket + entry_images table."""
    try:
        path = _path_from_url(image_url)
        if path:
            _sb().storage.from_(BUCKET).remove([path])
        _sb().table("entry_images").delete().eq("image_id", image_id).execute()
        return True
    except Exception:
        return False


def delete_image(image_url: str) -> bool:
    """Remove an image file from Supabase bucket. Safe to call with any string."""
    if not is_real_url(image_url):
        return False
    path = _path_from_url(image_url)
    if not path:
        return False
    try:
        _sb().storage.from_(BUCKET).remove([path])
        return True
    except Exception:
        return False


def _generate_and_save_worker(entry_id: str, user_prompt: str) -> None:
    try:
        _cleanup_old_image(entry_id)
        img = _generate_bytes(user_prompt)
        url = _upload_to_bucket(entry_id, img)
        _set_status(entry_id, url)
    except Exception as exc:
        _set_status(entry_id, f"failed: {exc}")


def trigger_async_generation(entry_id: str, user_prompt: str) -> None:
    """Set status to 'generating' immediately, then run in background thread."""
    if not user_prompt.strip():
        return
    _set_status(entry_id, STATUS_GENERATING)
    threading.Thread(
        target=_generate_and_save_worker,
        args=(entry_id, user_prompt),
        daemon=True,
    ).start()


def regenerate_sync(entry_id: str, user_prompt: str) -> Optional[str]:
    """Synchronous generation (used by edit form). Shows spinner in UI."""
    _cleanup_old_image(entry_id)
    _set_status(entry_id, STATUS_GENERATING)
    try:
        img = _generate_bytes(user_prompt)
        url = _upload_to_bucket(entry_id, img)
        _set_status(entry_id, url)
        return url
    except Exception as exc:
        _set_status(entry_id, f"failed: {exc}")
        st.error(f"Image generation failed: {exc}")
        return None
