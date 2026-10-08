"""Record a failure the moment it happens: durable, deduplicated, private.

One file per distinct failure, ``setup/health/events/<signature>.json``. The
signature is the component, the kind of failure, the error type and where in
the code it happened (or, for a refused file, which file), so the same crash
500 times is one entry with a count. Line numbers are left out of the
signature so it survives edits.

What a record keeps: a redacted message (``background.redact``: no URLs,
``name=secret`` pairs or token-like runs; paths under the state root, the
projects root and the home folder shortened), and code locations only
(file:line:function of this checkout's frames). Never local variables,
source text, request bodies or file contents.

``record()`` never raises: a bookkeeping failure must never change what its
caller does. It writes nothing until :func:`enable` (``session.begin``, which
only the server's lifespan calls): the record describes server runs, so a test,
script or tool that merely imports a store can never write into a real
``~/.quirq``. Repeats are coalesced in memory and written at most once per
signature every :data:`MIN_WRITE_INTERVAL_S`; :func:`flush` writes the rest
(the server calls it on shutdown).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.background import redact
from services.storage import layout
from services.storage.atomic_write import write_json_atomic
from services.timestamps import iso
from utils.safe_read import read_text_guarded

logger = logging.getLogger(__name__)

SCHEMA = 1

#: Retention (decision D3, docs: setup/health in the state README). A new
#: signature beyond these evicts the one seen longest ago.
MAX_EVENTS = 500
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_AGE_S = 30 * 86400
#: At most one disk write per signature in this window; repeats are counted
#: in memory meanwhile.
MIN_WRITE_INTERVAL_S = 10.0
#: The most recent occurrences kept in a record.
MAX_OCCURRENCES = 10
#: Innermost frames kept.
MAX_FRAMES = 20
#: A message is cut to this many characters.
MAX_MESSAGE = 200

CRASH, EXIT, FAILING, REFUSAL, HTTP_500, UNCLEAN_EXIT, FATAL = (
    "crash", "exit", "failing", "refusal", "http_500", "unclean_exit", "fatal")
KINDS = frozenset({CRASH, EXIT, FAILING, REFUSAL, HTTP_500, UNCLEAN_EXIT, FATAL})

#: Frames inside this checkout are kept with a path relative to it.
REPO_ROOT = Path(__file__).resolve().parents[2]

_lock = threading.Lock()
_enabled = False
_boot_id = "unknown"
_last_write: dict[str, float] = {}
_pending: dict[str, dict[str, Any]] = {}


def events_dir() -> Path:
    return layout.health_dir() / "events"


def enable() -> None:
    """Start writing (``session.begin`` calls it for a server run)."""
    global _enabled
    _enabled = True


def enabled() -> bool:
    return _enabled


def set_boot_id(boot_id: str) -> None:
    """The run each occurrence is stamped with (``session.begin`` sets it)."""
    global _boot_id
    _boot_id = boot_id


def record(component: str, kind: str, *, exc: Optional[BaseException] = None, message: Optional[str] = None,
           subject: Optional[str] = None, error_type: Optional[str] = None,
           frames: Optional[list[dict[str, Any]]] = None, details: Optional[dict[str, Any]] = None) -> None:
    """Record one failure. ``exc`` gives the type, message and code location;
    without it, ``message`` and ``error_type`` describe it. ``subject`` is
    what it concerns (a file, a route, a watcher step). Never raises."""
    if not _enabled:
        return
    try:
        _record(component, kind, exc, message, subject, error_type, frames, details, time.time())
    except Exception:  # noqa: BLE001 - recording must never cost the caller
        logger.debug("health: could not record %s/%s", component, kind, exc_info=True)


def flush() -> None:
    """Write every coalesced repeat now (shutdown). Never raises."""
    try:
        with _lock:
            for signature in list(_pending):
                _write(signature, time.time())
    except Exception:  # noqa: BLE001
        logger.debug("health: flush failed", exc_info=True)


def prune(now: Optional[float] = None) -> None:
    """Drop records not seen for :data:`MAX_AGE_S`. Never raises."""
    now = time.time() if now is None else now
    try:
        for path in _event_files():
            try:
                if now - path.stat().st_mtime > MAX_AGE_S:
                    path.unlink()
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        logger.debug("health: prune failed", exc_info=True)


# ── building a record ────────────────────────────────────────────────────────

def _record(component: str, kind: str, exc: Optional[BaseException], message: Optional[str],
            subject: Optional[str], error_type: Optional[str], frames: Optional[list[dict[str, Any]]],
            details: Optional[dict[str, Any]], now: float) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if frames is None:
        frames = code_frames(exc) if exc is not None else []
    error_type = error_type or (type(exc).__name__ if exc is not None else kind)
    text = message if message is not None else (str(exc) if exc is not None else "")
    location = (f"{frames[-1]['file']}:{frames[-1]['function']}" if frames else "") or clean(subject or "")
    signature = hashlib.sha1("|".join((component, kind, error_type, location)).encode("utf-8")).hexdigest()[:16]
    base = {
        "schema": SCHEMA, "signature": signature, "component": component, "kind": kind,
        "error_type": error_type, "message": clean(text), "subject": clean(subject) if subject else None,
        "frames": frames, "details": _clean_details(details),
    }
    with _lock:
        entry = _pending.setdefault(signature, {"count": 0, "occurrences": []})
        entry["base"] = base
        entry["count"] += 1
        entry["last_seen"] = now
        entry["occurrences"] = (entry["occurrences"] + [{"at": _stamp(now), "boot_id": _boot_id}])[-MAX_OCCURRENCES:]
        if now - _last_write.get(signature, 0.0) >= MIN_WRITE_INTERVAL_S:
            _write(signature, now)


def code_frames(exc: Optional[BaseException]) -> list[dict[str, Any]]:
    """Where it happened: this checkout's frames, innermost last; the
    innermost other frame only when none is ours. No locals, no source."""
    if exc is None or exc.__traceback__ is None:
        return []
    ours, last_other = [], None
    for frame in traceback.extract_tb(exc.__traceback__):
        relative = _relative(frame.filename)
        if relative is None:
            last_other = {"file": f"<lib>/{Path(frame.filename).name}", "line": frame.lineno, "function": frame.name}
        else:
            ours.append({"file": relative, "line": frame.lineno, "function": frame.name})
    if not ours and last_other is not None:
        ours = [last_other]
    return ours[-MAX_FRAMES:]


def _relative(filename: str) -> Optional[str]:
    try:
        return Path(filename).resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return None


def _roots() -> list[tuple[str, str]]:
    """Folders a message may name, longest first, and what each becomes."""
    pairs = []
    for raw, label in ((str(layout.quirq_state_dir()), "<state>"),
                       ((os.getenv("XO_PROJECTS_ROOT", "") or "").strip() or "~/xo-projects", "<projects>"),
                       (str(Path.home()), "<home>")):
        try:
            pairs.append((str(Path(raw).expanduser().resolve()), label))
        except (OSError, RuntimeError):
            continue
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def clean(text: Optional[str]) -> str:
    """A message safe to keep: folders shortened, secrets and URLs removed, cut."""
    text = str(text or "")
    for root, label in _roots():
        if root and root != "/":
            text = text.replace(root, label)
    return redact(text)[:MAX_MESSAGE]


def _clean_details(details: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not details:
        return {}
    return {str(key): clean(value) if isinstance(value, str) else value for key, value in details.items()
            if isinstance(value, (str, int, float, bool)) or value is None}


def _stamp(ts: float) -> str:
    return iso(datetime.fromtimestamp(ts, timezone.utc))


# ── writing, under _lock ─────────────────────────────────────────────────────

def _write(signature: str, now: float) -> None:
    entry = _pending.pop(signature, None)
    if entry is None:
        return
    path = events_dir() / f"{signature}.json"
    existing = _read(path)
    if existing is None:
        _make_room()
    document = dict(entry["base"])
    document["first_seen"] = (existing or {}).get("first_seen") or entry["occurrences"][0]["at"]
    document["last_seen"] = _stamp(entry["last_seen"])
    document["count"] = int((existing or {}).get("count") or 0) + entry["count"]
    previous = (existing or {}).get("occurrences")
    previous = previous if isinstance(previous, list) else []
    document["occurrences"] = (previous + entry["occurrences"])[-MAX_OCCURRENCES:]
    write_json_atomic(path, document)
    _last_write[signature] = now


def _read(path: Path) -> Optional[dict[str, Any]]:
    """An existing record, or None (absent, damaged or another version:
    a damaged record starts over rather than stopping the recording)."""
    try:
        document = json.loads(read_text_guarded(path, max_bytes=256 * 1024))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return None
    return document


def _event_files() -> list[Path]:
    try:
        return [path for path in events_dir().glob("*.json") if path.is_file() and not path.is_symlink()]
    except OSError:
        return []


def _make_room() -> None:
    """Before a new record: evict the ones seen longest ago until there is
    room under :data:`MAX_EVENTS` and :data:`MAX_TOTAL_BYTES`."""
    entries = []
    for path in _event_files():
        try:
            info = path.stat()
        except OSError:
            continue
        entries.append((info.st_mtime, info.st_size, path))
    entries.sort()
    total = sum(size for _, size, _ in entries)
    while entries and (len(entries) >= MAX_EVENTS or total > MAX_TOTAL_BYTES):
        _, size, path = entries.pop(0)
        try:
            path.unlink()
        except OSError:
            pass
        total -= size


def _reset_for_tests(*, enable_recording: bool = False) -> None:
    global _enabled
    with _lock:
        _pending.clear()
        _last_write.clear()
    set_boot_id("unknown")
    _enabled = enable_recording
