"""The per-connection files: ``~/.quirq/connections/<toolkit>/``.

One folder per polled toolkit, three hand-maintainable files:

* ``config.json``  what to collect and how often: ``enabled``,
                   ``interval_s`` (60 to 86400) and ``collectors`` (ids
                   from the catalog). Missing keys get defaults on read,
                   unknown keys survive a rewrite (read, merge, write).
* ``state.json``   poll cursors and the last result: ``last_poll_at``,
                   ``last_ok_at``, ``last_error`` (at most 300 chars),
                   ``cursors.<collector>.seen`` (newest :data:`SEEN_CAP`
                   keys) and ``events_total``.
* ``events.jsonl`` collected items, append-only, rotated at
                   :data:`_ROTATE_BYTES` into ``events.<stamp>.jsonl``
                   (the newest :data:`_MAX_ROTATIONS_KEEP` kept). Only the
                   live file is read; rotations are history.

Reads are lenient: a hand edit degrades to the default with a WARN. The
one exception is ``enabled``: the common spellings (``"false"``, ``0``,
``"no"``, ``"off"`` and their opposites) are accepted, and anything else
reads as off, so an unclear intent never keeps the poller calling the
upstream. Writes are strict and raise :class:`ConnectionsError`. Every write goes
through ``flock.locked`` on the file it touches, so the background poller
and a "poll now" from another process never clobber each other. Toolkit
ids are validated (``[a-z0-9_]{1,40}``) before any path is built, and
writes additionally require a toolkit the Composio catalog knows.

A folder is only ever created by :func:`write_config`. ``update_state``
and ``append_events`` refuse to write once ``config.json`` is gone, so a
DELETE racing a poll cannot resurrect the folder.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent.connectors.composio.service import TOOLKITS
from services.cowork_agent.inbox.store import parse_ts
from services.cowork_agent.local_state import quirq_state_dir
from services.cowork_agent.visualizer.atomic_write import append_jsonl, write_json_atomic
from services.cowork_agent.visualizer.flock import locked
from services.cowork_agent.visualizer.reader import read_json, read_jsonl_tail_reverse

from . import collectors

logger = logging.getLogger(__name__)

SCHEMA = 1
TOOLKIT_RE = re.compile(r"[a-z0-9_]{1,40}")
INTERVAL_MIN, INTERVAL_MAX, INTERVAL_DEFAULT = 60, 86400, 900
SEEN_CAP = 500
ERROR_MAX = 300
CONFIG_FIELDS = ("enabled", "interval_s", "collectors")
STATE_FIELDS = ("last_poll_at", "last_ok_at", "last_error", "cursors", "events_total")
_ROTATE_BYTES = 2 * 1024 * 1024      # tests patch this; never write 2 MB to exercise it
_MAX_ROTATIONS_KEEP = 3
_ROTATION_RE = re.compile(r"events\.\d{8}T\d{6}Z\.jsonl")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


class ConnectionsError(Exception):
    """Typed failure the router maps to ``HTTPException(status, {code, message})``."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Paths and toolkit validation ────────────────────────────────────────────


def _check_toolkit(toolkit) -> str:
    """The id must match the regex before it reaches a path join (``..``,
    ``/`` and friends are 404s, never filesystem touches)."""
    if not isinstance(toolkit, str) or TOOLKIT_RE.fullmatch(toolkit) is None:
        raise ConnectionsError("unknown_toolkit", f"Unknown toolkit {toolkit!r}.", 404)
    return toolkit


def _check_known(toolkit) -> str:
    """Writes need a toolkit the Composio catalog knows."""
    if _check_toolkit(toolkit) not in TOOLKITS:
        raise ConnectionsError("unknown_toolkit", f"Unknown toolkit {toolkit!r}.", 404)
    return toolkit


def connections_dir() -> Path:
    return quirq_state_dir() / "connections"


def connection_dir(toolkit: str) -> Path:
    return connections_dir() / _check_toolkit(toolkit)


def _config_path(toolkit: str) -> Path:
    return connection_dir(toolkit) / "config.json"


def list_configured() -> list[str]:
    """Folders holding a ``config.json``, sorted. Stray files, symlinks and
    names outside the regex are ignored."""
    root = connections_dir()
    if not root.is_dir():
        return []
    found: list[str] = []
    try:
        for child in root.iterdir():
            if (TOOLKIT_RE.fullmatch(child.name) and child.is_dir() and not child.is_symlink()
                    and (child / "config.json").is_file()):
                found.append(child.name)
    except OSError as exc:
        logger.warning("connections: could not list %s: %s", root, exc)
    return sorted(found)


# ── config.json ─────────────────────────────────────────────────────────────


def _coerce_enabled(value) -> Optional[bool]:
    """A bool, ``0``/``1``, or one of the usual words (case-insensitive);
    ``None`` for anything else so the caller can fail closed."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return None


def _coerce_interval(value) -> Optional[int]:
    """``int`` or a digit-only string in range; ``None`` otherwise (bool is
    an int subclass and is rejected on purpose)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, int) and INTERVAL_MIN <= value <= INTERVAL_MAX:
        return value
    return None


def _clean_collectors(toolkit: str, value, *, strict: bool) -> list[str]:
    """Ordered, deduplicated catalog ids. Lenient mode drops unknown ids with
    a WARN and falls back to the defaults for a non-list; strict mode raises."""
    known = {spec["id"] for spec in collectors.catalog(toolkit)}
    if not isinstance(value, list):
        if strict:
            raise ConnectionsError("invalid_collector", f"collectors must be a list of ids (got {value!r}).")
        logger.warning("connections: %s config.json collectors is not a list; using the defaults", toolkit)
        return collectors.default_ids(toolkit)
    out: list[str] = []
    for cid in value:
        if isinstance(cid, str) and cid in known:
            if cid not in out:
                out.append(cid)
        elif strict:
            raise ConnectionsError("invalid_collector",
                                   f"unknown collector {cid!r} for {toolkit}; available: {sorted(known)}.")
        else:
            logger.warning("connections: %s config.json drops unknown collector %r", toolkit, cid)
    return out


def _normalize_config(toolkit: str, raw: dict) -> dict:
    """Defaults for missing keys, lenient coercion for hand edits (WARN),
    unknown keys kept. The folder name wins over the file's ``toolkit``."""
    doc = dict(raw)
    enabled = _coerce_enabled(doc.get("enabled", True))
    if enabled is None:
        # Fail closed: a value nobody can read as yes or no must not keep
        # the poller calling the upstream every interval.
        logger.warning("connections: %s config.json enabled=%r is not a boolean; polling stays off until "
                       "it reads true or false", toolkit, doc.get("enabled"))
        enabled = False
    interval = doc.get("interval_s", INTERVAL_DEFAULT)
    cleaned = _coerce_interval(interval)
    if cleaned is None:
        logger.warning("connections: %s config.json interval_s=%r is not an integer in [%d, %d]; using %d",
                       toolkit, interval, INTERVAL_MIN, INTERVAL_MAX, INTERVAL_DEFAULT)
        cleaned = INTERVAL_DEFAULT
    if "collectors" in doc:
        chosen = _clean_collectors(toolkit, doc["collectors"], strict=False)
    else:
        chosen = collectors.default_ids(toolkit)
    doc.update({"schema": SCHEMA, "toolkit": toolkit, "enabled": enabled,
                "interval_s": cleaned, "collectors": chosen})
    doc.setdefault("updated_at", None)
    return doc


def read_config(toolkit: str) -> Optional[dict]:
    """The normalised config, or ``None`` when there is no ``config.json``
    (or it is not JSON: ``read_json`` already warned)."""
    raw = read_json(_config_path(toolkit))
    if not isinstance(raw, dict):
        return None
    return _normalize_config(toolkit, raw)


def _validate_field(toolkit: str, name: str, value):
    if name == "enabled":
        if not isinstance(value, bool):
            raise ConnectionsError("invalid_value", f"enabled must be true or false (got {value!r}).")
        return value
    if name == "interval_s":
        if isinstance(value, bool) or not isinstance(value, int) or not INTERVAL_MIN <= value <= INTERVAL_MAX:
            raise ConnectionsError("invalid_interval",
                                   f"interval_s must be an integer between {INTERVAL_MIN} and {INTERVAL_MAX} "
                                   f"seconds (got {value!r}).")
        return value
    if name == "collectors":
        return _clean_collectors(toolkit, value, strict=True)
    raise ConnectionsError("invalid_value", f"unknown config field {name!r}.")


def write_config(toolkit: str, **fields) -> dict:
    """Create the folder on first use, validate the given fields, then a
    locked read-merge-write. Keys the file already has and this call does
    not name survive; a file that is not JSON is refused, never overwritten.

    The merge starts from the normalised document, not the raw one, so a
    hand-edited value the lenient read already replaced (``interval_s`` 5,
    ``collectors`` "unread") is written back as what it read as, and the
    WARN stops firing on every read and every poller tick from then on."""
    _check_known(toolkit)
    clean = {name: _validate_field(toolkit, name, value) for name, value in fields.items()}
    path = _config_path(toolkit)
    with locked(path):
        raw = read_json(path)
        if raw is None and path.is_file() and path.read_text(encoding="utf-8").strip():
            raise ConnectionsError("config_unreadable",
                                   f"config.json for {toolkit} is not valid JSON; fix or remove it.", 500)
        current = _normalize_config(toolkit, raw) if isinstance(raw, dict) else {}
        defaults = {"enabled": True, "interval_s": INTERVAL_DEFAULT,
                    "collectors": collectors.default_ids(toolkit)}
        merged: dict = {"schema": SCHEMA, "toolkit": toolkit}
        for name in CONFIG_FIELDS:
            merged[name] = clean[name] if name in clean else current.get(name, defaults[name])
        for name, value in current.items():
            if name not in merged and name != "updated_at":
                merged[name] = value
        merged["updated_at"] = now_iso()
        write_json_atomic(path, merged)
    return _normalize_config(toolkit, merged)


# ── state.json ──────────────────────────────────────────────────────────────


def _state_path(toolkit: str) -> Path:
    return connection_dir(toolkit) / "state.json"


def _normalize_state(raw) -> dict:
    doc = dict(raw) if isinstance(raw, dict) else {}
    out: dict = {"schema": SCHEMA}
    for name in ("last_poll_at", "last_ok_at"):
        out[name] = doc[name] if isinstance(doc.get(name), str) and doc[name] else None
    err = doc.get("last_error")
    out["last_error"] = err[:ERROR_MAX] if isinstance(err, str) and err else None
    cursors: dict = {}
    if isinstance(doc.get("cursors"), dict):
        for name, cur in doc["cursors"].items():
            if not isinstance(name, str):
                continue
            seen = cur.get("seen") if isinstance(cur, dict) else None
            cursors[name] = {"seen": [k for k in seen if isinstance(k, str)] if isinstance(seen, list) else []}
    out["cursors"] = cursors
    total = doc.get("events_total")
    out["events_total"] = total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else 0
    for name, value in doc.items():
        out.setdefault(name, value)      # unknown keys survive
    return out


def read_state(toolkit: str) -> dict:
    return _normalize_state(read_json(_state_path(toolkit)))


def update_state(toolkit: str, **fields) -> dict:
    """Locked merge of the given fields. Refuses (returns the defaults) once
    ``config.json`` is gone: a DELETE must not be undone by a poll in flight."""
    _check_toolkit(toolkit)
    unknown = sorted(set(fields) - set(STATE_FIELDS))
    if unknown:
        raise ConnectionsError("invalid_value", f"unknown state field(s) {unknown}.")
    path = _state_path(toolkit)
    with locked(path):
        if not (path.parent / "config.json").is_file():
            logger.info("connections: %s has no config.json; state not written", toolkit)
            return _normalize_state(None)
        doc = _normalize_state(read_json(path))
        doc.update(fields)
        doc = _normalize_state(doc)
        write_json_atomic(path, doc)
    return doc


def remember_seen(state_doc: dict, collector: str, keys) -> list[str]:
    """Append ``keys`` to the collector's ``seen`` list (a key seen again
    moves to the end) and keep the newest :data:`SEEN_CAP`. In place."""
    cursors = state_doc.setdefault("cursors", {})
    cur = cursors.get(collector)
    if not isinstance(cur, dict):
        cur = {"seen": []}
        cursors[collector] = cur
    fresh: list[str] = []
    for key in keys or []:
        if isinstance(key, str) and key not in fresh:
            fresh.append(key)
    fresh_set = set(fresh)
    kept = [k for k in cur.get("seen", []) if isinstance(k, str) and k not in fresh_set]
    cur["seen"] = (kept + fresh)[-SEEN_CAP:]
    return cur["seen"]


# ── events.jsonl ────────────────────────────────────────────────────────────


def _events_path(toolkit: str) -> Path:
    return connection_dir(toolkit) / "events.jsonl"


def _rotate_if_needed(path: Path) -> None:
    """Rename the live file past the threshold and prune old rotations.
    Called inside the events lock. Only names in the fixed stamp format
    are ever pruned, so a hand-copied file is left alone."""
    try:
        if not path.is_file() or path.stat().st_size < _ROTATE_BYTES:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path.rename(path.with_name(f"events.{stamp}.jsonl"))
        rotations = sorted(p for p in path.parent.iterdir() if _ROTATION_RE.fullmatch(p.name))
        for old in rotations[:-_MAX_ROTATIONS_KEEP]:
            old.unlink()
    except OSError as exc:
        logger.warning("connections: events rotation failed for %s: %s", path.parent.name, exc)


def _ts_key(event: dict) -> datetime:
    return parse_ts(event.get("ts")) or _EPOCH


def append_events(toolkit: str, events: list[dict]) -> int:
    """Rotation check, then append. Collectors hand over batches newest-first
    while the tail reader returns file order reversed, so the batch is put
    into chronological order first (a stable sort over the reversed batch:
    ties keep their relative order) and the newest line lands last."""
    _check_known(toolkit)
    lines = [dict(e) for e in events or [] if isinstance(e, dict)]
    if not lines:
        return 0
    ordered = sorted(reversed(lines), key=_ts_key)
    path = _events_path(toolkit)
    with locked(path):
        if not (path.parent / "config.json").is_file():
            logger.warning("connections: %s has no config.json; %d event(s) dropped", toolkit, len(ordered))
            return 0
        _rotate_if_needed(path)
        append_jsonl(path, ordered)
    return len(ordered)


def read_events(toolkit: str, limit: int = 50, types=None) -> list[dict]:
    """Newest-first from the live file only. ``types`` is an iterable of
    collector ids, or ``None`` for all. The slice is re-sorted by ``ts`` as
    a safety net against interleaved late writers (stable, so ties keep
    file order reversed)."""
    _check_toolkit(toolkit)
    allow = None if types is None else frozenset(t for t in types if isinstance(t, str))
    rows = read_jsonl_tail_reverse(_events_path(toolkit), limit=max(1, int(limit)), types=allow)
    return sorted(rows, key=_ts_key, reverse=True)


# ── Removal ─────────────────────────────────────────────────────────────────


def remove(toolkit: str) -> bool:
    """``shutil.rmtree`` of that one folder, only when it is a real directory
    sitting directly under :func:`connections_dir` (no symlink, no escape).
    ``False`` when there is nothing to remove."""
    _check_toolkit(toolkit)
    root = connections_dir()
    target = root / toolkit
    if target.is_symlink() or not target.is_dir():
        return False
    try:
        resolved, root_resolved = target.resolve(strict=True), root.resolve(strict=True)
    except OSError:
        return False
    if resolved.parent != root_resolved:
        logger.warning("connections: refusing to remove %s (outside %s)", toolkit, root)
        return False
    assert root_resolved in resolved.parents, "remove() target escaped connections_dir()"
    shutil.rmtree(resolved)
    return True
