"""The person's own layer of the UI's shape (xo-coworker ``js/shape.js``):
one small JSON document, kept in the store so it follows the person to every
device.

XO keeps it and doesn't read it: its keys are the UI's (merged by the UI over
a preset with JSON Merge Patch). The server only holds it to what a layer
is: a JSON object of at most ``MAX_BYTES``, nested at most ``MAX_DEPTH``.
Writes are compare-and-set when the client sends ``if_version`` (the
``version`` it read; ``"new"`` when it read none), so two devices never
silently overwrite each other; without it the write wins. Each change is a
``settings {key, value}`` event, so the person's other open clients hear it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from services.cowork_agent.storm.chats.router import read_setting, write_setting
from services.cowork_agent.storm.engine import Engine, Writes
from services.errors import ServiceError

KEY = "ui.shape"
NEW = "new"
MAX_BYTES = 64 * 1024
MAX_DEPTH = 12


class ShapeRefused(ServiceError):
    pass


def version(doc: Any) -> str:
    """A short hash of the stored document; ``"new"`` when there is none."""
    if doc is None:
        return NEW
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]


def _view(conn) -> dict[str, Any]:
    stored = read_setting(conn, KEY, None)
    doc = stored.get("doc") if isinstance(stored, Mapping) else None
    return {"shape": doc, "version": version(doc),
            "updated_at": stored.get("updated_at") if isinstance(stored, Mapping) else None}


async def get(engine: Engine) -> dict[str, Any]:
    return await engine.store.read(_view)


def _depth(value: Any, level: int = 0) -> int:
    if isinstance(value, Mapping):
        return max((_depth(v, level + 1) for v in value.values()), default=level + 1)
    if isinstance(value, list):
        return max((_depth(v, level + 1) for v in value), default=level + 1)
    return level


async def put(engine: Engine, doc: Mapping[str, Any] | None, *, if_version: str | None = None) -> dict[str, Any]:
    """Store the person's layer (None clears it)."""
    if doc is not None:
        if not isinstance(doc, Mapping):
            raise ShapeRefused("bad_shape", "The shape is a JSON object.", status=400)
        size = len(json.dumps(doc, separators=(",", ":")).encode("utf-8"))
        if size > MAX_BYTES:
            raise ShapeRefused("shape_too_large", f"The shape is {size} bytes; at most {MAX_BYTES} are kept.",
                               status=413)
        if _depth(doc) > MAX_DEPTH:
            raise ShapeRefused("bad_shape", f"The shape nests deeper than {MAX_DEPTH} levels.", status=400)

    def save(w: Writes) -> dict[str, Any]:
        current = _view(w)
        if if_version is not None and if_version != current["version"]:
            error = ShapeRefused("shape_changed", "Your shape changed on another device since this one read it; "
                                                  "read it again, then save.", status=409)
            error.current = current
            raise error
        if version(doc) != current["version"]:
            write_setting(w, KEY, {"doc": doc, "updated_at": w.now} if doc is not None else None)
        return _view(w)
    return await engine.write(save)
