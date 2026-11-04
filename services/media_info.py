"""What a client needs to lay out a file before its bytes arrive: an image's
width, height and average colour (``tint``). Used for the person's uploads
and for the files agents share alike.

Blocking (it opens the file); call it off the event loop. Only the header is
read for the size. The tint decodes a reduced copy (JPEG's draft mode, or a
thumbnail), and only for images up to ``TINT_MAX_PIXELS``: a decompression
bomb gets its size and no tint. Anything Pillow can't read (SVG, a damaged
file) gives ``None`` for each field, never an error.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The images whose average colour is worked out (40 megapixels).
TINT_MAX_PIXELS = 40_000_000
#: The side the image is reduced to before averaging.
TINT_SAMPLE = 64
NONE: dict[str, Any] = {"w": None, "h": None, "tint": None}


def image_info(path: str | Path, mime: str | None = None) -> dict[str, Any]:
    """``{w, h, tint}`` of an image file (``tint`` as ``#rrggbb``); each
    ``None`` when it can't be read. Not an image by ``mime``: all ``None``."""
    if mime is not None and not mime.startswith("image/"):
        return dict(NONE)
    try:
        from PIL import Image
    except ImportError:                     # Pillow is in requirements.txt; a broken install still answers
        return dict(NONE)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                w, h = image.size
                exif = image.getexif() if hasattr(image, "getexif") else None
                if exif and exif.get(0x0112) in (5, 6, 7, 8):   # rotated a quarter turn: shown the other way
                    w, h = h, w
                tint = _tint(image) if w * h <= TINT_MAX_PIXELS else None
    except Exception as exc:  # noqa: BLE001 - Pillow raises many kinds for a file it can't read
        log.debug("media info: %s isn't a readable image: %s", path, exc)
        return dict(NONE)
    return {"w": int(w), "h": int(h), "tint": tint}


def _tint(image) -> str | None:
    try:
        if image.format == "JPEG":
            image.draft("RGB", (TINT_SAMPLE, TINT_SAMPLE))
        sample = image.convert("RGBA")
        sample.thumbnail((TINT_SAMPLE, TINT_SAMPLE))
        flat = getattr(sample, "get_flattened_data", None)     # Pillow 12.1+; getdata() is deprecated there
        pixels = list(flat() if flat else sample.getdata())
    except Exception:  # noqa: BLE001
        return None
    weight = sum(p[3] for p in pixels)
    if not weight:
        return None
    r, g, b = (round(sum(p[i] * p[3] for p in pixels) / weight) for i in range(3))
    return f"#{r:02x}{g:02x}{b:02x}"
