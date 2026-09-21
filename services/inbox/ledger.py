"""The feeders' ledger: ``~/.quirq/inbox/ledger.json`` (under ``QUIRQ_STATE_ROOT``).

Since 2026-09-21 the Inbox keeps no rows of its own: a fact that arrives
becomes a work item at ingestion (:mod:`services.inbox.facts`), and the
work item is the record. What the Inbox still has to remember is the
feeders' bookkeeping, and that is this file: one cursor per feeder (the
newest producer timestamp read, kept as the producer's original string)
and the per-source switches. Hand-editable; unknown keys survive a rewrite.

::

    {"schema": 1, "updated_at": "...",
     "cursors": {"sharing": "...", "issues": "...", "connections": "..."},
     "sources": {"sharing": {"enabled": true}, "issues": {"enabled": true, "states": ["open"]},
                 "connections": {"enabled": true}}}
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Callable, Optional

from services.errors import ServiceError
from services.storage.atomic_write import write_json_atomic
from services.storage.flock import locked
from services.storage.layout import inbox_dir
from services.storage.reader import read_json
from services.timestamps import now_iso, parse_ts

__all__ = ["SCHEMA", "DEFAULT_SOURCES", "InboxError", "ledger_path", "normalize_document", "source_config",
           "load_document", "modify", "parse_ts"]

logger = logging.getLogger(__name__)

SCHEMA = 1
DEFAULT_SOURCES: dict = {
    "sharing": {"enabled": True},
    "issues": {"enabled": True, "states": ["open"]},
    "connections": {"enabled": True},
}


class InboxError(ServiceError):
    """A typed failure of the Inbox (``code``, ``message``, ``status``)."""


def ledger_path() -> Path:
    return inbox_dir() / "ledger.json"


def _normalize_source(name: str, raw) -> dict:
    cfg = copy.deepcopy(DEFAULT_SOURCES[name])
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key == "enabled":
                cfg["enabled"] = value is not False
            elif key == "states":
                cfg["states"] = [s for s in value if isinstance(s, str) and s] if isinstance(value, list) else cfg["states"]
            else:
                cfg[key] = value
    return cfg


def normalize_document(raw) -> dict:
    """The ledger with every key present. A malformed value falls back to
    its default; unknown top-level keys are kept."""
    doc = dict(raw) if isinstance(raw, dict) else {}
    out = {k: v for k, v in doc.items() if k not in ("schema", "updated_at", "cursors", "sources", "items")}
    out["schema"] = SCHEMA
    out["updated_at"] = doc.get("updated_at") if isinstance(doc.get("updated_at"), str) else None
    cursors = doc.get("cursors") if isinstance(doc.get("cursors"), dict) else {}
    out["cursors"] = {name: (value if isinstance(value, str) and parse_ts(value) else None)
                      for name, value in cursors.items() if isinstance(name, str)}
    for name in DEFAULT_SOURCES:
        out["cursors"].setdefault(name, None)
    sources = doc.get("sources") if isinstance(doc.get("sources"), dict) else {}
    out["sources"] = {name: _normalize_source(name, sources.get(name)) for name in DEFAULT_SOURCES}
    return out


def source_config(doc: dict, name: str) -> dict:
    """The feeder's switches, with the defaults filled in."""
    sources = doc.get("sources") if isinstance(doc.get("sources"), dict) else {}
    return _normalize_source(name, sources.get(name)) if name in DEFAULT_SOURCES else {"enabled": True}


def load_document(path: Optional[Path] = None) -> tuple[dict, bool]:
    """``(document, ok)``: a missing file is the empty ledger (``ok``); a
    file that is not a JSON object reads as the empty ledger with ``ok``
    false, so a caller never overwrites a hand edit it could not parse."""
    path = path or ledger_path()
    if not path.is_file():
        return normalize_document(None), True
    try:
        raw = read_json(path)
    except Exception:  # noqa: BLE001 - read_json is defensive already; a torn file must not raise here
        raw = None
    if not isinstance(raw, dict):
        logger.warning("inbox ledger: %s is not a JSON object; the feeders start from their defaults", path)
        return normalize_document(None), False
    return normalize_document(raw), True


def modify(fn: Callable[[dict], bool], *, path: Optional[Path] = None) -> dict:
    """Locked read-modify-write. ``fn`` edits the normalised document in
    place and returns whether anything changed; the file is rewritten
    only then, and never when it could not be parsed."""
    path = path or ledger_path()
    with locked(path):
        doc, ok = load_document(path)
        if not ok:
            raise InboxError("ledger_unreadable", f"{path.name} is not valid JSON; fix or remove it.", 500)
        if fn(doc):
            doc["updated_at"] = now_iso()
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, doc)
        return doc
