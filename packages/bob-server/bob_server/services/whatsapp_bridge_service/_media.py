"""Module-level helpers for the WhatsApp bridge service.

Pure functions for phone/JID formatting, timestamp formatting, GIF resizing,
and media-prep (resize + thumbnail). Extracted from the original
``whatsapp_bridge_service.py`` so callers (notably ``whatsapp_outreach_tools``)
can import ``_prepare_media`` without pulling in the whole service class.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _jid_to_phone(jid: str) -> str:
    """Extract phone number from WhatsApp JID and normalize to +CC format."""
    phone_part = jid.split("@")[0] if "@" in jid else jid
    phone_part = phone_part.split(":")[0] if ":" in phone_part else phone_part
    digits = re.sub(r"\D", "", phone_part)
    if phone_part.startswith("+"):
        return "+" + digits
    # Assume Australian number if no country code
    if digits.startswith("0"):
        return "+61" + digits[1:]
    if digits.startswith("61"):
        return "+" + digits
    if len(digits) > 8:
        return "+" + digits


_PERTH = ZoneInfo("Australia/Perth")


def _format_created_at(iso_utc: str) -> str:
    """Render a UTC ISO timestamp as Perth-local 'YYYY-MM-DD HH:MM'."""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(_PERTH).strftime("%Y-%m-%d %H:%M")


# The Go bridge has a 1 MB WebSocket read limit.
# base64 adds ~33% overhead, so raw image bytes must stay well under 750 KB.
_BRIDGE_MAX_PAYLOAD_BYTES = 700_000
_WHATSAPP_MAX_DIMENSION = 1920


def _resize_gif(path: str) -> str | None:
    """Downsize an animated GIF by dropping frames and scaling, preserving animation."""
    import tempfile
    from PIL import Image

    img = Image.open(path)
    if not getattr(img, "is_animated", False):
        # Not actually animated — treat as static
        img.close()
        return None

    frames = []
    durations = []
    n_frames = img.n_frames
    # Start by skipping every other frame, increase skip if still too large
    for skip in (1, 2, 3, 4):
        frames.clear()
        durations.clear()
        for i in range(0, n_frames, skip + 1):
            img.seek(i)
            frame = img.copy()
            w, h = frame.size
            if max(w, h) > _WHATSAPP_MAX_DIMENSION:
                ratio = _WHATSAPP_MAX_DIMENSION / max(w, h)
                frame = frame.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            frames.append(frame)
            durations.append(img.info.get("duration", 100))

        if not frames:
            img.close()
            return None

        from io import BytesIO
        buf = BytesIO()
        frames[0].save(
            buf, format="GIF", save_all=True,
            append_images=frames[1:], duration=durations, loop=img.info.get("loop", 0),
            optimize=True,
        )
        for f in frames:
            f.close()
        if buf.tell() <= _BRIDGE_MAX_PAYLOAD_BYTES:
            tmp = tempfile.NamedTemporaryFile(suffix=".gif", delete=False)
            tmp.write(buf.getvalue())
            tmp.close()
            img.close()
            return tmp.name

    img.close()
    return None


async def _prepare_media(path: str) -> str | None:
    """Resize/convert an image to fit WhatsApp and bridge WebSocket limits. Returns path to send."""
    import mimetypes

    mime = (mimetypes.guess_type(path)[0] or "").lower()
    lower_path = path.lower()

    if not mime.startswith("image/") and not lower_path.endswith(".gif"):
        return path

    is_gif = mime == "image/gif" or lower_path.endswith(".gif")
    file_size = os.path.getsize(path)

    # GIFs: if within limits, send as-is to preserve animation
    if is_gif:
        if file_size <= _BRIDGE_MAX_PAYLOAD_BYTES:
            return path
        # Too large — try to reduce frames (returns None for non-animated GIFs)
        import functools
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, functools.partial(_resize_gif, path))
            if result is not None:
                return result
            # Non-animated or resize failed — fall through to static path
        except Exception:
            logger.exception("failed to resize gif %s", path)

    # Static images
    needs_resize = file_size > _BRIDGE_MAX_PAYLOAD_BYTES
    if not needs_resize:
        try:
            from PIL import Image
            with Image.open(path) as img:
                w, h = img.size
            if max(w, h) <= _WHATSAPP_MAX_DIMENSION:
                return path
        except Exception:
            return path

    import functools
    import tempfile

    def _resize() -> str | None:
        from io import BytesIO
        from PIL import Image

        img = Image.open(path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        w, h = img.size
        dim = min(max(w, h), _WHATSAPP_MAX_DIMENSION)
        for quality in (85, 70, 55, 40):
            ratio = dim / max(w, h)
            scaled = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
            buf = BytesIO()
            scaled.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= _BRIDGE_MAX_PAYLOAD_BYTES:
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp.write(buf.getvalue())
                tmp.close()
                img.close()
                return tmp.name
            dim = int(dim * 0.75)

        img.close()
        return None

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, functools.partial(_resize))
    except Exception:
        logger.exception("failed to resize image %s", path)
        return None

