"""The timeline's surface: one envelope, open types, every line written once.

Every line is ``{ts, type, pid?, project_id?, session_id?, runtime?, ...}``
(:func:`envelope`). ``type`` must be one of :func:`declared_types`: the
schema's enum (the 21 legacy types) plus every module's ``events.TYPES``.
:func:`emit` validates, stamps ``project_id`` and ``pid`` when given, and
writes each line once: to the project's log when it carries a pid (or when
the caller names the runtime ``key`` of a project that has none yet), else
to the Space log. :func:`read` answers one project's log, or the merged Space
view: the tail of every project log plus the Space log, newest first,
deduped against the copies older installs made. :func:`compact_space_log`
drops those copies from the Space log once per process, lazily, before the
first merged read.

Knows nothing about HTTP: a bad argument is a :class:`ServiceError` the
app's handler turns into ``{"detail": {"code", "message"}}``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Iterable, Optional

from services import modules as registry
from services.cowork_agent import project_layout
from services.errors import NotFound, ServiceError
from services.storage import eventlog
from services.storage.flock import locked
from services.storage.reader import read_jsonl_tail_reverse
from services.timestamps import parse_ts

from . import store

logger = logging.getLogger(__name__)

#: The keys every line may open with, in this order, after ``ts`` and ``type``.
ENVELOPE_KEYS = ("pid", "project_id", "session_id", "runtime")
LIMIT_DEFAULT = 200
LIMIT_MAX = 500

#: The schema whose ``type`` enum declares the legacy event types.
SCHEMA_PATH = (Path(__file__).resolve().parents[2] / "services" / "cowork_agent"
               / "visualizer" / "schema" / "timeline.schema.json")

_schema_types_cache: Optional[frozenset[str]] = None
#: Space logs this process has compacted (by path: a test sandbox is a new path).
_compacted: set[str] = set()


# ── The envelope and the declared types ──────────────────────────────────────


def envelope(line: dict) -> dict:
    """``ts`` and ``type`` first, then ``pid``, ``project_id``,
    ``session_id`` and ``runtime`` when present, then the rest in the order
    they came."""
    base = eventlog.envelope(line)
    head = {k: base[k] for k in ("ts", "type")}
    for key in ENVELOPE_KEYS:
        if key in base:
            head[key] = base[key]
    return {**head, **{k: v for k, v in base.items() if k not in head}}


def _schema_types() -> frozenset[str]:
    """The ``type`` enum of ``timeline.schema.json`` (or, for an older
    shape, the ``const`` of every ``oneOf`` branch). Read once."""
    global _schema_types_cache
    if _schema_types_cache is not None:
        return _schema_types_cache
    found: set[str] = set()
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a packaging accident, not a branch
        logger.warning("timeline: %s unreadable; only module-declared types are accepted", SCHEMA_PATH.name)
        schema = {}
    enum = ((schema.get("properties") or {}).get("type") or {}).get("enum")
    if isinstance(enum, list):
        found.update(t for t in enum if isinstance(t, str) and t)
    definitions = schema.get("definitions") or {}
    for branch in schema.get("oneOf") or []:
        ref = branch.get("$ref", "") if isinstance(branch, dict) else ""
        node = definitions.get(ref.rsplit("/", 1)[-1]) if ref else branch
        const = ((node.get("properties") or {}).get("type") or {}).get("const") if isinstance(node, dict) else None
        if isinstance(const, str) and const:
            found.add(const)
    _schema_types_cache = frozenset(found)
    return _schema_types_cache


def declared_types() -> frozenset[str]:
    """Every event type a line may carry: the schema's enum plus every
    module's ``events.TYPES`` (``services.modules.event_types``)."""
    return _schema_types() | frozenset(registry.event_types())


def check_types(types: Optional[Iterable[str] | str]) -> Optional[frozenset[str]]:
    """A comma-separated string or an iterable of type names to the set a
    read filters on; ``None`` (or nothing named) means every type. An
    undeclared type is refused with 400 ``invalid_value``."""
    if types is None:
        return None
    if isinstance(types, str):
        names = {t.strip() for t in types.split(",") if t.strip()}
    else:
        names = {str(t).strip() for t in types if str(t).strip()}
    if not names:
        return None
    unknown = names - declared_types()
    if unknown:
        raise ServiceError("invalid_value", f"unknown timeline type(s): {sorted(unknown)}")
    return frozenset(names)


def clamp_limit(limit) -> int:
    """``limit`` as an int in 1..:data:`LIMIT_MAX`; anything unreadable is
    the default."""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = LIMIT_DEFAULT
    return max(1, min(LIMIT_MAX, value))


# ── Writing ──────────────────────────────────────────────────────────────────


def _check_key(key, *, what: str = "pid") -> Optional[str]:
    """``key`` when it is a safe runtime key (one path segment under the
    runtime home), else ``None`` (logged)."""
    if key is None:
        return None
    try:
        store.project_log(key)
    except (ValueError, TypeError):
        logger.warning("timeline: refusing %s %r: not a safe runtime key", what, key)
        return None
    return str(key)


def emit(lines: Iterable[dict], *, project_id: Optional[str] = None,
         pid: Optional[str] = None, key: Optional[str] = None) -> list[dict]:
    """Validate, stamp and write ``lines``, each exactly once.

    A line needs a parseable ``ts`` and a declared ``type``; one without is
    refused with a logged warning and skipped, never written. ``project_id``
    and ``pid`` are stamped on every line when given. A line carrying a pid
    (stamped or its own) goes to that project's log, ``projects/<pid>/``.
    ``key`` files the lines that carry none under ``projects/<key>/``
    without stamping anything: the runtime home of a project that has no
    pid yet is keyed by its folder name, and its lines are its own. With
    neither, a line goes to the Space log. Returns the lines written, in
    envelope order.
    """
    declared = declared_types()
    stamped_pid = _check_key(pid)
    if pid is not None and stamped_pid is None:
        return []
    fallback = _check_key(key, what="key")
    if key is not None and fallback is None:
        return []
    by_target: dict[Optional[str], list[dict]] = {}
    written: list[dict] = []
    for raw in lines:
        if not isinstance(raw, dict):
            logger.warning("timeline: refusing a line that is not an object (%s)", type(raw).__name__)
            continue
        line = dict(raw)
        if project_id:
            line["project_id"] = project_id
        if stamped_pid:
            line["pid"] = stamped_pid
        if parse_ts(line.get("ts")) is None:
            logger.warning("timeline: refusing a %r line without a timestamp", line.get("type"))
            continue
        kind = line.get("type")
        if not isinstance(kind, str) or kind not in declared:
            logger.warning("timeline: refusing a line of undeclared type %r (declare it in a module's events.TYPES)", kind)
            continue
        target = line.get("pid")
        if target is not None:
            if not isinstance(target, str) or _check_key(target) is None:
                continue
            line["pid"] = target
        else:
            line.pop("pid", None)
            target = fallback
        ordered = envelope(line)
        by_target.setdefault(target, []).append(ordered)
        written.append(ordered)
    for target, batch in by_target.items():
        log = store.project_log(target) if target else store.space_log()
        log.append(batch)
    return written


# ── Reading ──────────────────────────────────────────────────────────────────


def _dedupe_key(line: dict) -> str:
    """The line without ``project_id``, in a canonical form: what a project
    line and the copy an older install put in the Space log share."""
    return json.dumps({k: v for k, v in line.items() if k != "project_id"},
                      sort_keys=True, ensure_ascii=False, default=str)


def _project_names() -> dict[str, str]:
    """runtime key (the pid, or the folder name for a project without one)
    to the project's folder name, for tagging merged lines."""
    out: dict[str, str] = {}
    for meta in project_layout.list_projects():
        name = meta.get("name")
        if not isinstance(name, str) or not name:
            continue
        try:
            out[project_layout.runtime_key(name)] = name
        except Exception:  # noqa: BLE001 - one unreadable project must not hide the others
            logger.debug("timeline: could not resolve the runtime key of %s", name, exc_info=True)
    return out


def _resolve_project(project_id: str) -> str:
    """A project's folder name to the runtime key its log is filed under."""
    root = project_layout.runtime_dir_for_project(project_id)
    if root is None:
        raise NotFound("project_not_found", f"Unknown project {project_id!r}.")
    return root.name


def _check_before(before: Optional[str]) -> Optional[str]:
    if before is None or before == "":
        return None
    if parse_ts(before) is None:
        raise ServiceError("invalid_value", "before must be an ISO-8601 timestamp")
    return before


def read(*, limit: int = LIMIT_DEFAULT, before: Optional[str] = None,
         types: Optional[Iterable[str]] = None, project_id: Optional[str] = None,
         pid: Optional[str] = None) -> list[dict]:
    """Newest first, at most ``limit`` lines.

    With ``pid`` (or ``project_id``, resolved through ``project_layout``)
    one project's log. Otherwise the merged Space view: the tail of every
    project log plus the Space log, each line from a project log tagged
    with the project's current folder name, deduped so a line and the copy
    an older install made of it count once. ``before`` (an ISO timestamp)
    and ``types`` (an allowlist) filter; an unknown project is a 404
    ``project_not_found``.
    """
    wanted = max(1, int(limit))
    cutoff = _check_before(before)
    allow = None if types is None else frozenset(t for t in types if isinstance(t, str))
    if pid is None and project_id:
        pid = _resolve_project(project_id)
    if pid is not None:
        try:
            log = store.project_log(pid)
        except (ValueError, TypeError):
            raise NotFound("project_not_found", f"Unknown project {pid!r}.") from None
        return [envelope(line) for line in log.tail(limit=wanted, before=cutoff, types=allow)]

    compact_space_log()
    names = _project_names()
    seen: set[str] = set()
    merged: list[dict] = []

    def take(line: dict, key: Optional[str]) -> None:
        tagged = dict(line)
        if key is not None:
            name = names.get(key) or tagged.get("project_id")
            if name:
                tagged["project_id"] = name
        ordered = envelope(tagged)
        digest = _dedupe_key(ordered)
        if digest in seen:
            return
        seen.add(digest)
        merged.append(ordered)

    for key, log in store.project_logs().items():
        for line in log.tail(limit=wanted, before=cutoff, types=allow):
            take(line, key)
    for line in store.space_log().tail(limit=wanted, before=cutoff, types=allow):
        take(line, None)
    merged.sort(key=eventlog.ts_key, reverse=True)
    return merged[:wanted]


# ── Compaction of the Space log ──────────────────────────────────────────────


def _all_lines(log: eventlog.EventLog) -> list[dict]:
    """Every line of a log, live file and rotations."""
    out: list[dict] = []
    for path in [log.path, *log.rotations()]:
        out.extend(read_jsonl_tail_reverse(path, limit=sys.maxsize))
    return out


def compact_space_log() -> int:
    """Drop from ``projects/timeline.jsonl`` the copies of project lines an
    older install put there (a line carrying a pid whose project log holds
    the same line, ``project_id`` aside). Once per process and per path,
    under the log's lock, rewritten atomically; a no-op when no line carries
    a pid. A pid-carrying line the project log does not hold is kept: the
    Space log is history, and nothing here may lose a line. Returns how
    many lines were dropped.
    """
    log = store.space_log()
    marker = str(log.path)
    if marker in _compacted:
        return 0
    _compacted.add(marker)
    if not log.path.is_file():
        return 0
    dropped = 0
    try:
        with locked(log.path):
            raw_lines = log.path.read_text(encoding="utf-8").splitlines()
            if not any('"pid"' in raw for raw in raw_lines):
                return 0
            keys_by_pid: dict[str, set[str]] = {}
            kept: list[str] = []
            for raw in raw_lines:
                if not raw.strip():
                    continue
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    kept.append(raw)
                    continue
                pid = line.get("pid") if isinstance(line, dict) else None
                if isinstance(pid, str) and pid:
                    if pid not in keys_by_pid:
                        try:
                            keys_by_pid[pid] = {_dedupe_key(l) for l in _all_lines(store.project_log(pid))}
                        except (ValueError, TypeError):
                            keys_by_pid[pid] = set()
                    if _dedupe_key(line) in keys_by_pid[pid]:
                        dropped += 1
                        continue
                kept.append(raw)
            if not dropped:
                return 0
            tmp = log.path.with_suffix(log.path.suffix + ".tmp")
            tmp.write_text("".join(raw + "\n" for raw in kept), encoding="utf-8")
            os.replace(tmp, log.path)
    except OSError as exc:
        logger.warning("timeline: could not compact the Space log: %s", exc)
        return 0
    logger.info("timeline: compacted the Space log: dropped %d line(s) that live in project logs", dropped)
    return dropped


def reset_for_tests() -> None:
    """Forget which Space logs were compacted and the cached schema types."""
    global _schema_types_cache
    _compacted.clear()
    _schema_types_cache = None
