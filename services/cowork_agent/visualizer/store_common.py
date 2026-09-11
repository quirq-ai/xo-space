"""
Shapes shared by the stores that own an authored document — workitems, peers
and the runtime claims file. Each keeps its own schema, validation and error
type; only what was identical across them lives here.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    write_json_owned,
)

#: The identity charset every document key, session key and assignee shares:
#: permissive enough for colon-separated composite keys, restrictive enough to
#: reject traversal and anything that could turn a key into caller text.
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_:\-\.]{1,200}$")


class StoreError(Exception):
    """``(code, message)`` — ``code`` is the BFF error code a route serves."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
