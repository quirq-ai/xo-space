"""What each long-running background task of this server is doing.

``server.py`` registers every long-running task it starts; the loops that
drive them (``services.periodic.run_forever``, the watcher) record each tick.
The doctor reads :func:`snapshot` to say "the watcher crashed at 20:27:05
with RuntimeError: …" instead of "last ticked 3 minutes ago".

Space-level: every component uses it (the project's placement rule). It only
records: nothing here restarts, cancels or changes a task. No function
raises; a bookkeeping bug must never stop the loop it observes.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

#: The longest error text kept. Errors reach the Health panel.
ERROR_MAX = 200
_URL = re.compile(r"https?://\S+")
_QUERY_SECRET = re.compile(r"\b[\w-]*(?:token|secret|key|password)[\w-]*=\S+", re.IGNORECASE)
#: Runs of 24+ word characters look like keys and tokens, not prose.
_TOKEN_LIKE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def redact(text: str) -> str:
    """``text`` with URLs, ``name=secret`` pairs and token-like runs replaced,
    cut to :data:`ERROR_MAX` characters."""
    text = _URL.sub("<url>", str(text))
    text = _QUERY_SECRET.sub("<redacted>", text)
    text = _TOKEN_LIKE.sub("<redacted>", text)
    return text[:ERROR_MAX]


def describe(exc: BaseException) -> str:
    """``TypeName: message`` (or just the type), redacted."""
    message = str(exc)
    return redact(f"{type(exc).__name__}: {message}" if message else type(exc).__name__)


@dataclass
class _Record:
    name: str
    started_at: float
    finishes_by_design: bool = False
    state: str = "running"
    ended_at: Optional[float] = None
    error: Optional[str] = None
    ticks: int = 0
    last_tick_started_at: Optional[float] = None
    last_tick_ok_at: Optional[float] = None
    consecutive_failures: int = 0
    last_failure: Optional[str] = None
    last_failure_at: Optional[float] = None


_lock = threading.Lock()
_records: dict[str, _Record] = {}


def register(name: str, task: Any, *, finishes_by_design: bool = False) -> None:
    """Start recording ``task`` under ``name``. ``finishes_by_design`` marks a
    task whose returning is normal (a one-off sweep), not a failure."""
    try:
        record = _Record(name=name, started_at=time.time(), finishes_by_design=finishes_by_design)
        with _lock:
            _records[name] = record
        task.add_done_callback(lambda done, record=record: _ended(record, done))
    except Exception:  # noqa: BLE001 - bookkeeping must never break startup
        pass


def _ended(record: _Record, task: Any) -> None:
    """Runs on the event loop when the task finishes, however it finishes."""
    try:
        if task.cancelled():
            state, error = "cancelled", None
        else:
            exc = task.exception()
            state, error = ("returned", None) if exc is None else ("crashed", describe(exc))
        with _lock:
            record.state, record.error, record.ended_at = state, error, time.time()
    except Exception:  # noqa: BLE001
        pass


def _get(name: str) -> Optional[_Record]:
    return _records.get(name)


def tick_started(name: str) -> None:
    try:
        with _lock:
            record = _get(name)
            if record is not None:
                record.last_tick_started_at = time.time()
    except Exception:  # noqa: BLE001
        pass


def tick_succeeded(name: str) -> None:
    try:
        with _lock:
            record = _get(name)
            if record is not None:
                record.ticks += 1
                record.last_tick_ok_at = time.time()
                record.consecutive_failures = 0
    except Exception:  # noqa: BLE001
        pass


def tick_failed(name: str, error: Any) -> None:
    """``error`` is the exception, or a text a loop composed itself."""
    try:
        text = describe(error) if isinstance(error, BaseException) else redact(str(error))
        with _lock:
            record = _get(name)
            if record is not None:
                record.ticks += 1
                record.consecutive_failures += 1
                record.last_failure = text
                record.last_failure_at = time.time()
    except Exception:  # noqa: BLE001
        pass


def snapshot() -> dict[str, dict[str, Any]]:
    """A copy of every record, safe to read from any thread."""
    try:
        with _lock:
            return {name: asdict(record) for name, record in _records.items()}
    except Exception:  # noqa: BLE001
        return {}


def reset_for_tests() -> None:
    with _lock:
        _records.clear()
