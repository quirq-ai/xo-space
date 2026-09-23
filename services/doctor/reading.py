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

from services.doctor.model import size

#: A bad JSON file modified this recently may be an in-place write in progress;
#: it is left out of this run (architecture §7.4).
RECENT_WRITE_S = 5
#: Entries one walk visits before it gives up and says so.
MAX_WALK_ENTRIES = 50_000
#: Files past this size are not read (the same bound as checks.MAX_FILE_BYTES).
#: State files are small; a runaway one must not cost the server its memory.
#: Read from the module at call time, so tests can lower it.
MAX_READ_BYTES = 50 * 1024 * 1024


def _special_kind(mode: int) -> str:
    if stat.S_ISFIFO(mode):
        return "a pipe"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "a device"
    return "a special file"


#: How much one os.read() asks for. A single read(MAX_READ_BYTES + 1) reserves
#: the whole limit up front, even for a 12-byte file.
READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class ReadResult:
    outcome: str
    detail: str = ""
    value: Optional[dict] = None
    schema: Any = None


def _read_regular(path: Path) -> "bytes | bytearray | ReadResult":
    """The bytes of ``path``, at most ``MAX_READ_BYTES`` of them, or the
    ReadResult that says why not. Raises OSError as open() and read() do."""
    # O_NONBLOCK and the fstat close the gap in which the path could be
    # swapped for a FIFO or a folder after the caller's stat; on a regular
    # file both are no-ops.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            return ReadResult("unreadable", "Is a directory")
        if not stat.S_ISREG(mode):
            return ReadResult("special", _special_kind(mode))
        # One growing buffer, not a list of chunks joined at the end, so the
        # peak stays near one copy of the file rather than two.
        raw = bytearray()
        while True:
            chunk = os.read(fd, READ_CHUNK_BYTES)
            if not chunk:
                return raw
            raw += chunk
            if len(raw) > MAX_READ_BYTES:
                # The file grew past the limit since the caller's stat.
                return ReadResult("file_too_large", f"over {size(MAX_READ_BYTES)}")
    finally:
        os.close(fd)


def classify(path: Path, *, now: float, accepted: Optional[frozenset[int]],
             stamped: bool = True) -> ReadResult:
    """``accepted`` is the set of schema versions this xo-space reads, or
    ``None`` for a file exempt from stamping. ``stamped`` is False for a
    document whose own schema leaves ``schema`` out of ``required``: an absent
    version there means the lowest accepted one, not a refused file."""
    try:
        # stat() never blocks and never reads; everything that could is
        # refused here, before any open(). A FIFO's open() waits for a writer
        # forever, and a device such as /dev/zero reports st_size 0 and never
        # ends, so the type check (not the size cap) is what protects the
        # server from both.
        info = os.stat(path)
        if stat.S_ISDIR(info.st_mode):
            return ReadResult("unreadable", "Is a directory")
        if not stat.S_ISREG(info.st_mode):
            return ReadResult("special", _special_kind(info.st_mode))
        if info.st_size > MAX_READ_BYTES:
            return ReadResult("file_too_large", size(info.st_size))
        raw = _read_regular(path)
    except FileNotFoundError:
        return ReadResult("absent")
    except OSError as exc:
        return ReadResult("unreadable", exc.strerror or type(exc).__name__)
    if isinstance(raw, ReadResult):
        return raw
    mtime = info.st_mtime
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
    except RecursionError:
        # Thousands of nested brackets exhaust the parser's recursion limit
        # instead of raising a decode error.
        return ReadResult("recent") if recent else ReadResult("invalid_json", "nested too deeply to read")
    except ValueError:
        # int() refuses a number longer than sys.get_int_max_str_digits().
        return ReadResult("recent") if recent else ReadResult("invalid_json", "holds a number too long to read")
    if not isinstance(value, dict):
        return ReadResult("wrong_type", type(value).__name__)
    if accepted is None:
        return ReadResult("ok", value=value)
    if not stamped and "schema" not in value:
        # Only an ABSENT key is legitimate; a present but malformed stamp
        # ("1", null, true) is still refused below.
        return ReadResult("ok", value=value, schema=min(accepted))
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
