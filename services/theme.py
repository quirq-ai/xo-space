"""The workspace's saved visual theme, separate from its name and logo."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from services.errors import ServiceError
from services.storage.atomic_write import write_json_atomic
from services.storage.flock import locked
from services.storage.layout import settings_dir

DEFAULT_THEME = "space"
THEMES = frozenset({"space", "quirq"})
_WRITE_LOCK = threading.Lock()


class ThemeError(ServiceError):
    """A theme validation or persistence failure that is safe to display."""


def _path() -> Path:
    return settings_dir() / "theme.json"


def _valid_theme(value: object) -> bool:
    return isinstance(value, str) and value in THEMES


def _read() -> dict:
    try:
        document = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": 1, "theme": DEFAULT_THEME}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ThemeError(
            "theme_unavailable",
            "The saved theme could not be read. Check workspace storage and try again.",
            503,
        ) from exc
    if (
        not isinstance(document, dict)
        or type(document.get("schema")) is not int
        or document["schema"] != 1
        or not _valid_theme(document.get("theme"))
    ):
        raise ThemeError(
            "theme_unavailable",
            "The saved theme is invalid. Check workspace storage before saving changes.",
            503,
        )
    return document


def get_theme() -> dict:
    """Read the theme without creating any state for the default."""
    return {"theme": _read()["theme"]}


def save_theme(theme: str) -> dict:
    """Persist a supported theme without changing saved branding."""
    if not _valid_theme(theme):
        raise ThemeError("invalid_theme", "Choose the Grove or Neon theme.", 422)
    try:
        path = _path()
        with _WRITE_LOCK, locked(path):
            document = _read()
            document["theme"] = theme
            write_json_atomic(path, document)
    except OSError as exc:
        raise ThemeError(
            "theme_save_failed",
            "The theme could not be saved. Check workspace storage and try again.",
            503,
        ) from exc
    return {"theme": document["theme"]}
