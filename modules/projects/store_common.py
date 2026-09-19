"""
Shapes shared by the stores that own an authored document — workitems, peers
and the runtime claims file. Each keeps its own schema, validation and error
type; only what was identical across them lives here.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Type, TypeVar

_StoreErrorT = TypeVar("_StoreErrorT", bound="StoreError")

from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    unsupported_schema_message,
    write_json_owned,
)
from services.errors import ServiceError
from services.storage.document import CORRUPT_DOCUMENT_MESSAGE, UNSUPPORTED_SCHEMA_MESSAGE

#: The identity charset every document key, session key and assignee shares:
#: permissive enough for colon-separated composite keys, restrictive enough to
#: reject traversal and anything that could turn a key into caller text.
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-\.]{1,200}$")


class StoreError(ServiceError):
    """``(code, message)`` plus the status the raise site chose: a caller
    error is the 400 default, a missing record 404, a refused document 409
    (:func:`refused_document`, :func:`unsupported_schema`). The app handler
    serves it as ``{"code", "message"}``; no route maps it."""

    def __init__(self, code: str, message: str, status: int = 400, *, log: Optional[str] = None) -> None:
        super().__init__(code, message, status, log=log)


class Unset:
    """Sentinel: "the caller did not supply this field"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


UNSET = Unset()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ordered(record: dict, key_order: Iterable[str]) -> dict:
    """``record`` with its keys in ``key_order``, extras kept at the end."""
    out = {key: record[key] for key in key_order if key in record}
    for key, value in record.items():
        if key not in out:
            out[key] = value
    return out


def corrupt_message(path: Path, reason: str, *, document: str, loss: str) -> str:
    """The O-E refusal: why an unreadable document is not treated as empty."""
    return (
        f"{path} is not a readable {document} document ({reason}); refusing to "
        f"read or write it. Treating it as empty would discard {loss} "
        f"(docs/OUTSTANDING.md O-E). Repair or move the file."
    )


def refused_document(
    cls: Type[_StoreErrorT], path: Path, reason: str, *, name: str, document: str, loss: str,
) -> _StoreErrorT:
    """The O-E refusal as the store's own 409 ``corrupt_document``: the
    shared wording (naming ``name``, e.g. ``"todos.json"``) on the wire, the
    path-bearing :func:`corrupt_message` in the log only."""
    return cls(
        "corrupt_document",
        CORRUPT_DOCUMENT_MESSAGE.format(name=name),
        409,
        log=corrupt_message(path, reason, document=document, loss=loss),
    )


def unsupported_schema(
    cls: Type[_StoreErrorT], path: Path, found: Any, expected: int, *, name: str,
) -> _StoreErrorT:
    """A document stamped by a newer writer, as the store's own 409
    ``unsupported_schema``; same split between wire and log."""
    return cls(
        "unsupported_schema",
        UNSUPPORTED_SCHEMA_MESSAGE.format(name=name),
        409,
        log=unsupported_schema_message(path, found, expected),
    )


def write_owned(
    path: Path,
    *,
    owns: frozenset[str],
    values: dict[str, Any],
    corrupt: Callable[[Path, str], Exception],
) -> None:
    """
    :func:`write_json_owned`, with a merge base that went unreadable raised as
    the caller's own corrupt-document error.
    """
    try:
        write_json_owned(path, owns=owns, values=values)
    except CorruptDocumentError as exc:
        raise corrupt(path, exc.reason) from exc
