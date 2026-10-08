"""This run's marker, so the next start knows whether it ended cleanly.

At boot, :func:`begin` reads the last run's ``setup/health/session.json``:
one with no ``clean_exit_at`` ended without shutting down (kill -9, out of
memory, a crash, power loss) and is recorded as ``unclean_exit``, unless that
process is still running (another server on the same state folder). A
traceback ``faulthandler`` left in ``fatal.log`` (a segfault or abort) is
recorded as ``fatal``. Then the new run's marker and annotations are written
and ``faulthandler`` is pointed at ``fatal.log``.

:func:`mark_alive` refreshes ``alive_at`` at most once a minute, so "ended
around" is close; :func:`end` marks a clean shutdown. Nothing here raises.
"""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.health import annotations, recorder
from services.storage import layout
from services.storage.atomic_write import write_json_atomic
from services.timestamps import iso, parse_ts
from utils.safe_read import read_text_guarded

logger = logging.getLogger(__name__)

ALIVE_EVERY_S = 60.0
#: Boot annotations kept.
MAX_BOOTS = 20
#: The most of a fatal.log that is read back.
FATAL_READ_BYTES = 256 * 1024
_FATAL_FRAME = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+) in (?P<function>\S+)')

_fatal_handle = None
_last_alive = 0.0


def session_path() -> Path:
    return layout.health_dir() / "session.json"


def boots_dir() -> Path:
    return layout.health_dir() / "boots"


def fatal_path() -> Path:
    return layout.health_dir() / "fatal.log"


def _stamp(ts: float) -> str:
    return iso(datetime.fromtimestamp(ts, timezone.utc))


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        document = json.loads(read_text_guarded(path, max_bytes=256 * 1024))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _still_running(pid: Any) -> bool:
    """The previous run's process is another server that is still up (it
    shares this state folder), not this one and not a recycled pid."""
    if not isinstance(pid, int) or pid == os.getpid():
        return False
    try:
        return b"server.py" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def _ended_around(previous: dict[str, Any]) -> Optional[str]:
    """The last moment the previous run is known to have been alive."""
    candidates = [parse_ts(previous.get("alive_at")), parse_ts(previous.get("started_at"))]
    heartbeat = _read_json(layout.cache_dir() / "heartbeat.json")
    if heartbeat is not None:
        candidates.append(parse_ts(heartbeat.get("last_tick_at")))
    known = [moment for moment in candidates if moment is not None]
    return iso(max(known)) if known else None


def begin(now: Optional[float] = None) -> str:
    """Start this run's record; returns its boot id. Never raises."""
    now = time.time() if now is None else now
    boot_id = uuid.uuid4().hex[:12]
    try:
        recorder.set_boot_id(boot_id)
        previous = _read_json(session_path())
        if previous is not None and not previous.get("clean_exit_at") and not _still_running(previous.get("pid")):
            recorder.record("server", recorder.UNCLEAN_EXIT, error_type="UncleanExit",
                            message="The previous run ended without shutting down.",
                            details={"previous_boot_id": previous.get("boot_id"),
                                     "previous_started_at": previous.get("started_at"),
                                     "ended_around": _ended_around(previous),
                                     "previous_version": previous.get("version")})
        _record_fatal_log()
        started_at = _stamp(now)
        notes = annotations.collect(boot_id, started_at)
        write_json_atomic(boots_dir() / f"{boot_id}.json", notes)
        _prune_boots()
        recorder.prune(now)
        write_json_atomic(session_path(), {
            "schema": recorder.SCHEMA, "boot_id": boot_id, "pid": os.getpid(), "started_at": started_at,
            "alive_at": started_at, "version": notes.get("version"), "clean_exit_at": None})
        _enable_faulthandler()
    except Exception:  # noqa: BLE001 - the record must never stop a boot
        logger.warning("health: could not start this run's record", exc_info=True)
    global _last_alive
    _last_alive = now
    return boot_id


def mark_alive(now: Optional[float] = None) -> None:
    """Refresh ``alive_at`` at most every :data:`ALIVE_EVERY_S`. Never raises."""
    global _last_alive
    now = time.time() if now is None else now
    if now - _last_alive < ALIVE_EVERY_S:
        return
    _last_alive = now
    _update(alive_at=_stamp(now))


def end(now: Optional[float] = None) -> None:
    """A clean shutdown: write coalesced repeats, mark the run clean. Never raises."""
    now = time.time() if now is None else now
    recorder.flush()
    _update(alive_at=_stamp(now), clean_exit_at=_stamp(now))
    _disable_faulthandler()


def _update(**fields: Any) -> None:
    try:
        current = _read_json(session_path())
        if current is None:
            return
        current.update(fields)
        write_json_atomic(session_path(), current)
    except Exception:  # noqa: BLE001
        logger.debug("health: could not update this run's record", exc_info=True)


def _prune_boots() -> None:
    try:
        boots = sorted((path for path in boots_dir().glob("*.json") if path.is_file()),
                       key=lambda path: path.stat().st_mtime)
        for path in boots[:-MAX_BOOTS]:
            path.unlink()
    except OSError:
        pass


def _record_fatal_log() -> None:
    """A traceback the last run's faulthandler left becomes a ``fatal``
    record; the log then moves to ``fatal.log.1`` so it is read once."""
    path = fatal_path()
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return
        text = read_text_guarded(path, max_bytes=None, errors="replace")[-FATAL_READ_BYTES:]
    except OSError:
        return
    lines = [line for line in text.splitlines() if line.strip()]
    headline = next((line.strip() for line in lines if line.startswith("Fatal Python error")), None)
    frames = []
    for line in lines:
        match = _FATAL_FRAME.match(line)
        if match:
            relative = recorder._relative(match["file"])
            frames.append({"file": relative or f"<lib>/{Path(match['file']).name}",
                           "line": int(match["line"]), "function": match["function"]})
    if headline is not None or frames:
        # faulthandler prints the innermost frame first.
        recorder.record("server", recorder.FATAL, error_type="FatalError",
                        message=headline or "The server stopped with a fatal error.",
                        frames=list(reversed(frames))[-recorder.MAX_FRAMES:])
    try:
        path.replace(path.with_name("fatal.log.1"))
    except OSError:
        pass


def _enable_faulthandler() -> None:
    global _fatal_handle
    path = fatal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _fatal_handle = open(path, "a", encoding="utf-8")  # kept open: faulthandler writes to its fd
    faulthandler.enable(file=_fatal_handle, all_threads=True)


def _disable_faulthandler() -> None:
    global _fatal_handle
    try:
        if _fatal_handle is not None:
            faulthandler.disable()
            _fatal_handle.close()
    except Exception:  # noqa: BLE001
        pass
    _fatal_handle = None
