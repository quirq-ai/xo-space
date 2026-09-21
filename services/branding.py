"""Workspace display name and logo, saved together as one atomic setting."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import threading
import unicodedata
from pathlib import Path

from services.errors import ServiceError
from services.storage.atomic_write import write_json_atomic
from services.storage.flock import locked
from services.storage.layout import settings_dir

DEFAULT_NAME = "Space"
MAX_NAME_LENGTH = 80
MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_LOGO_DIMENSION = 4096
_MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_WRITE_LOCK = threading.Lock()


class BrandingError(ServiceError):
    """A branding validation or persistence failure that is safe to display."""


def _path() -> Path:
    return settings_dir() / "branding.json"


def _name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > MAX_NAME_LENGTH:
        raise BrandingError("invalid_branding_name", "Enter a name between 1 and 80 characters.", 422)
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise BrandingError("invalid_branding_name", "The workspace name cannot contain control characters.", 422)
    return value


def _verified_logo(content: bytes) -> dict:
    if len(content) > MAX_LOGO_BYTES:
        raise BrandingError("branding_logo_too_large", "The logo must be 2 MB or smaller.", 413)
    if not content:
        raise BrandingError("invalid_branding_logo", "Choose a PNG, JPEG, or WebP image.", 422)
    try:
        from PIL import Image, UnidentifiedImageError
    except ModuleNotFoundError as exc:
        if exc.name != "PIL":
            raise
        raise BrandingError(
            "branding_image_support_unavailable",
            "Logo uploads require Pillow. Install the updated backend requirements and try again.",
            503,
        ) from exc
    try:
        with Image.open(io.BytesIO(content)) as image:
            media_type = _MEDIA_TYPES.get(image.format)
            if media_type is None:
                raise BrandingError("invalid_branding_logo", "Choose a PNG, JPEG, or WebP image.", 422)
            if max(image.size) > MAX_LOGO_DIMENSION:
                raise BrandingError("branding_logo_dimensions", "The logo must be at most 4096 × 4096 pixels.", 422)
            image.verify()
        # verify() checks the file structure; decoding also rejects truncated
        # raster data that otherwise has a valid header (notably JPEG/WebP).
        with Image.open(io.BytesIO(content)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise BrandingError("invalid_branding_logo", "The image could not be read. Choose a valid PNG, JPEG, or WebP image.", 422) from exc
    return {"data": base64.b64encode(content).decode("ascii"), "media_type": media_type}


def _logo_bytes(logo: dict) -> bytes:
    if not isinstance(logo, dict) or logo.get("media_type") not in _MEDIA_TYPES.values():
        raise ValueError("Invalid stored logo")
    data = logo.get("data")
    if not isinstance(data, str) or len(data) > 4 * ((MAX_LOGO_BYTES + 2) // 3):
        raise ValueError("Invalid stored logo")
    content = base64.b64decode(data, validate=True)
    if not content or len(content) > MAX_LOGO_BYTES:
        raise ValueError("Invalid stored logo")
    return content


def _read() -> dict:
    try:
        document = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": 1, "name": DEFAULT_NAME, "logo": None}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrandingError("branding_unavailable", "The saved branding could not be read. Try again after checking workspace storage.", 503) from exc
    try:
        if not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != 1:
            raise ValueError("Unsupported branding document")
        if not isinstance(document.get("name"), str) or _name(document["name"]) != document["name"]:
            raise ValueError("Invalid stored name")
        if document.get("logo") is not None:
            _logo_bytes(document["logo"])
    except (ValueError, binascii.Error, BrandingError) as exc:
        raise BrandingError("branding_unavailable", "The saved branding is invalid. Check workspace storage before saving changes.", 503) from exc
    return document


def _public(document: dict) -> dict:
    logo = document.get("logo")
    version = hashlib.sha256(_logo_bytes(logo)).hexdigest() if logo else None
    return {
        "name": document["name"],
        "logo_url": f"/space/branding/logo?v={version}" if version else None,
    }


def get_branding() -> dict:
    """Read the saved identity without creating any state for the default."""
    return _public(_read())


def save_branding(name: str, logo: bytes | None = None, remove_logo: bool = False) -> dict:
    """Validate before changing disk, preserving the existing logo on a rename."""
    name = _name(name)
    if logo is not None and remove_logo:
        raise BrandingError("invalid_branding_logo", "Upload a logo or remove the current logo, one at a time.", 422)
    new_logo = _verified_logo(logo) if logo is not None else None
    try:
        path = _path()
        with _WRITE_LOCK, locked(path):
            document = _read()
            document["name"] = name
            if logo is not None or remove_logo:
                document["logo"] = new_logo
            # One JSON commit keeps the name and image consistent even if a
            # write fails. No upload filename or external path is ever used.
            write_json_atomic(path, document)
    except OSError as exc:
        raise BrandingError("branding_save_failed", "Branding could not be saved. Check workspace storage and try again.", 503) from exc
    return _public(document)


def get_logo() -> tuple[bytes, str, str]:
    """Return the current verified raster and its content version."""
    logo = _read().get("logo")
    if logo is None:
        raise BrandingError("branding_logo_not_found", "No custom workspace logo has been uploaded.", 404)
    content = _logo_bytes(logo)
    return content, logo["media_type"], hashlib.sha256(content).hexdigest()
