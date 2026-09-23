"""Reading the disk without trusting it.

``classify`` puts every JSON read into exactly one outcome, so "can't read"
is never confused with "read garbage" (03 §2.3, F7). Results never carry file
content except the parsed object itself, which checks use internally and
never copy into a report beyond ids, schema numbers and sizes.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: A bad JSON file modified this recently may be an in-place write in progress;
#: it is left out of this run (architecture §7.4).
RECENT_WRITE_S = 5
#: Entries one walk visits before it gives up and says so.
MAX_WALK_ENTRIES = 50_000


@dataclass(frozen=True)
class ReadResult:
    outcome: str
    detail: str = ""
    value: Optional[dict] = None
    schema: Any = None


def classify(path: Path, *, now: float, accepted: Optional[frozenset[int]]) -> ReadResult:
    """``accepted`` is the set of schema versions this xo-space reads, or
    ``None`` for a file exempt from stamping."""
    try:
        mtime = os.stat(path).st_mtime
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return ReadResult("absent")
    except OSError as exc:
        return ReadResult("unreadable", exc.strerror or type(exc).__name__)
    # A file dated AHEAD of the clock is not a write in progress: the negative
    # difference would otherwise make it "recent" forever, and a corrupt keep
    # file would never be reported. Restored backups, copied state roots and
    # NFS or container clock skew all produce future mtimes.
    recent = 0 <= now - mtime < RECENT_WRITE_S
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ReadResult("recent") if recent else ReadResult("invalid_json", "not UTF-8")
    if not text.strip():
        return ReadResult("recent") if recent else ReadResult("empty")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        # Position only: exc.msg is a fixed phrase, never the file's text.
        detail = f"{exc.msg} at line {exc.lineno} column {exc.colno}"
        return ReadResult("recent") if recent else ReadResult("invalid_json", detail)
    if not isinstance(value, dict):
        return ReadResult("wrong_type", type(value).__name__)
    if accepted is None:
        return ReadResult("ok", value=value)
    found = value.get("schema")
    if isinstance(found, bool) or not isinstance(found, int):
        return ReadResult("schema_unsupported", "missing", value=value, schema=None)
    if found in accepted:
        return ReadResult("ok", value=value, schema=found)
    direction = "newer" if found > max(accepted) else "older"
    return ReadResult("schema_unsupported", direction, value=value, schema=found)


@dataclass(frozen=True)
class Tree:
    bytes: int
    files: int
    newest: Optional[float]
    truncated: bool


def measure_tree(path: Path, limit: int = MAX_WALK_ENTRIES) -> Tree:
    """Total size, file count and newest mtime under ``path``, without
    following symlinks. The age is unknown (``newest`` is ``None``) past
    ``limit`` entries, or when any part of the tree couldn't be listed or
    stat'd; ``bytes``/``files`` still count what was seen either way."""
    total = files = seen = 0
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        # The root itself vanished or can't be stat'd: undated, not a crash.
        return Tree(0, 0, None, False)
    unreadable = False

    def _onerror(_exc: OSError) -> None:
        nonlocal unreadable
        unreadable = True

    for dirpath, dirnames, filenames in os.walk(path, followlinks=False, onerror=_onerror):
        for name in (*dirnames, *filenames):
            seen += 1
            if seen > limit:
                return Tree(total, files, None, True)
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                unreadable = True
                continue
            newest = max(newest, info.st_mtime)
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                files += 1
    return Tree(total, files, None if unreadable else newest, False)


def readable_dir(path: Path) -> bool:
    try:
        return Path(path).is_dir() and os.access(path, os.R_OK | os.X_OK)
    except OSError:
        return False
