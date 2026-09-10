"""Fixed-interval command scheduler: the job store and the tick.

Design: docs/superpowers/specs/2026-09-11-command-scheduler-design.md.

An agent registers a command plus an interval and goes away. Something
long-lived (the visualizer watcher, once per tick) calls :func:`tick`, which
launches whatever is due through ``utils.commands`` and harvests whatever
finished. The tick is synchronous, thread-safe and idempotent: state is
written to disk *before* a job is launched, so calling ``tick`` twice with
the same ``now`` starts nothing the second time.

Files, all under ``<quirq state>/scheduler/`` (mode 0600 where supported):

    jobs.json        definitions — written only by registration
    state.json       next_run / last_run / running_since / last_result —
                     written only by the tick and by run_now
    runs/<id>.jsonl  append-only run history, newest last
    logs/<id>.log    the executor's own log of every run (full output)

Two files, two owners: a re-registration cannot clobber a ``next_run`` and a
tick cannot clobber a definition. The scheduler never imports the watcher
and never imports the visualizer package; it only takes a timestamp.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from services.cowork_agent.local_state import quirq_state_dir
from utils.commands import CommandResult, CommandSpec, run_spec_sync

logger = logging.getLogger(__name__)

SCHEMA = 1
ENV_ENABLED = "XO_SCHEDULER_ENABLED"
ENV_MAX_CONCURRENT = "XO_SCHEDULER_MAX_CONCURRENT"
DEFAULT_MAX_CONCURRENT = 4
MIN_INTERVAL_SECONDS = 60
OUTPUT_TAIL_CHARS = 2000

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
_DEFINITION_KEYS = frozenset({"name", "command", "every_seconds", "project_id", "enabled"})


class SchedulerError(Exception):
    """A scheduler file cannot be read, or an operation is not valid now."""


class UnknownJobError(SchedulerError):
    """No job with that id."""


class JobRunningError(SchedulerError):
    """The job already has a run in progress (single-flight)."""


# ── Paths ────────────────────────────────────────────────────────────────────


def scheduler_dir() -> Path:
    return quirq_state_dir() / "scheduler"


def jobs_file() -> Path:
    return scheduler_dir() / "jobs.json"


def state_file() -> Path:
    return scheduler_dir() / "state.json"


def runs_file(job_id: str) -> Path:
    return scheduler_dir() / "runs" / f"{job_id}.jsonl"


def log_file(job_id: str) -> Path:
    return scheduler_dir() / "logs" / f"{job_id}.log"


# ── Configuration (read at call time, like the GitHub poller's switches) ─────


def scheduler_enabled() -> bool:
    raw = (os.getenv(ENV_ENABLED) or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def max_concurrent() -> int:
    raw = (os.getenv(ENV_MAX_CONCURRENT) or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_CONCURRENT
    except ValueError:
        return DEFAULT_MAX_CONCURRENT
    return max(1, value)


# ── Time ─────────────────────────────────────────────────────────────────────
# Wall-clock, UTC, second granularity — the stamp format the watcher sinks
# write. Durations are the executor's business (it uses the monotonic clock).


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_STAMP)


def parse_stamp(text: str) -> datetime:
    return datetime.strptime(text, _STAMP).replace(tzinfo=timezone.utc)


def _resolve_now(now: Optional[datetime]) -> datetime:
    """``now`` is injectable so tests drive the scheduler with fixed times."""
    if now is None:
        return now_utc()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.replace(microsecond=0)


def advance(next_run: datetime, every_seconds: int, now: datetime) -> datetime:
    """The first slot on this job's grid strictly after ``now``.

    Fixed-rate: slots stay on ``created_at + k * every`` so a slow run does
    not drift the schedule. Missed slots collapse — a machine that was off
    for three days gets one run, not three.
    """
    if next_run > now:
        return next_run
    missed = int((now - next_run).total_seconds() // every_seconds) + 1
    return next_run + timedelta(seconds=every_seconds * missed)


# ── Documents ────────────────────────────────────────────────────────────────


def _empty_doc() -> dict:
    return {"schema": SCHEMA, "jobs": {}}


def _read_doc(path: Path) -> dict:
    """Absent → empty document. Corrupt → SchedulerError; never rewritten."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_doc()
    except OSError as exc:
        raise SchedulerError(f"{path} is not readable: {exc}") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchedulerError(
            f"{path} is not valid JSON ({exc}); repair or delete it — the scheduler "
            f"will not rewrite a file it cannot read"
        ) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        raise SchedulerError(f"{path} must be a JSON object with a 'jobs' object")
    return doc


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows, or a filesystem without modes


def _write_doc(path: Path, doc: dict) -> None:
    """Atomic replace. Deliberately not the visualizer's ``write_json_atomic``:
    the scheduler must not depend on the watcher's package — the dependency
    points the other way (the watcher calls us)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _chmod_private(tmp)
    os.replace(tmp, path)


def _append_run(job_id: str, record: dict) -> None:
    path = runs_file(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    _chmod_private(path)


# ── Definitions ──────────────────────────────────────────────────────────────


def validate_definition(payload: Any) -> dict:
    """Return a normalised definition (no id, no timestamps) or raise ValueError.

    The command is validated by ``CommandSpec.from_json`` — the same door the
    skill catalog and manifests use — and stored back as ``argv``, so the
    tick only ever sees a list. ``timeout`` is mandatory here even though the
    executor allows ``None``: a job without one could hold its running flag
    forever.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("job must be a JSON object")
    unknown = set(payload) - _DEFINITION_KEYS
    if unknown:
        raise ValueError(f"unknown job keys: {sorted(unknown)}")
    name = payload.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError("name must be 1-64 characters: letters, digits, space, '_', '.', '-'")
    if "command" not in payload:
        raise ValueError("command is required (a JSON object: argv, timeout, optional cwd/env)")
    spec = CommandSpec.from_json(payload["command"])  # CommandSpecError is a ValueError
    if spec.timeout is None:
        raise ValueError("command.timeout is required: a scheduled job without one could run forever")
    every = payload.get("every_seconds")
    if isinstance(every, bool) or not isinstance(every, int) or every < MIN_INTERVAL_SECONDS:
        raise ValueError(f"every_seconds must be an integer >= {MIN_INTERVAL_SECONDS}")
    project_id = payload.get("project_id")
    if project_id is not None and (not isinstance(project_id, str) or not project_id):
        raise ValueError("project_id must be a non-empty string when given")
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    command: dict[str, Any] = {"argv": spec.argv, "timeout": spec.timeout}
    if spec.cwd is not None:
        command["cwd"] = spec.cwd
    if spec.env is not None:
        command["env"] = spec.env
    return {
        "name": name,
        "project_id": project_id,
        "command": command,
        "every_seconds": every,
        "enabled": enabled,
    }


def _new_id(name: str, taken: Mapping[str, Any]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "job"
    while True:
        candidate = f"{slug}-{secrets.token_hex(3)}"
        if candidate not in taken:
            return candidate


def _initial_state(job: Mapping[str, Any], now: datetime) -> dict:
    return {
        "next_run": stamp(now + timedelta(seconds=int(job["every_seconds"]))),
        "last_run": None,
        "running_since": None,
        "last_result": None,
    }


# ── In-memory runs ───────────────────────────────────────────────────────────


@dataclass
class _Run:
    job_id: str
    trigger: str
    started_at: datetime
    thread: Optional[threading.Thread] = None
    result: Optional[CommandResult] = None
    finished_at: Optional[datetime] = None


_lock = threading.Lock()
_running: dict[str, _Run] = {}


def reset_state() -> None:
    """Forget in-memory runs. For tests."""
    with _lock:
        _running.clear()


def _view(job: Mapping[str, Any], entry: Optional[Mapping[str, Any]]) -> dict:
    """A job as the API shows it: definition merged flat with its state."""
    entry = entry or {"next_run": None, "last_run": None, "running_since": None, "last_result": None}
    return {**job, **entry, "running": job["id"] in _running}


# ── Registration ─────────────────────────────────────────────────────────────


def create_job(payload: Any, *, now: Optional[datetime] = None) -> dict:
    definition = validate_definition(payload)
    now = _resolve_now(now)
    with _lock:
        jobs = _read_doc(jobs_file())
        state = _read_doc(state_file())
        job_id = _new_id(definition["name"], jobs["jobs"])
        job = {"id": job_id, **definition, "created_at": stamp(now), "updated_at": stamp(now)}
        jobs["jobs"][job_id] = job
        state["jobs"][job_id] = _initial_state(job, now)
        _write_doc(jobs_file(), jobs)
        _write_doc(state_file(), state)
        return _view(job, state["jobs"][job_id])


def get_job(job_id: str) -> dict:
    with _lock:
        job = _read_doc(jobs_file())["jobs"].get(job_id)
        if job is None:
            raise UnknownJobError(job_id)
        return _view(job, _read_doc(state_file())["jobs"].get(job_id))


def list_jobs() -> list[dict]:
    with _lock:
        jobs = _read_doc(jobs_file())["jobs"]
        state = _read_doc(state_file())["jobs"]
        return [_view(jobs[k], state.get(k)) for k in sorted(jobs)]


def update_job(job_id: str, payload: Any, *, now: Optional[datetime] = None) -> dict:
    """Replace the definition, keep the id. A changed interval restarts the
    grid from now; anything else leaves ``next_run`` alone."""
    definition = validate_definition(payload)
    now = _resolve_now(now)
    with _lock:
        jobs = _read_doc(jobs_file())
        state = _read_doc(state_file())
        old = jobs["jobs"].get(job_id)
        if old is None:
            raise UnknownJobError(job_id)
        job = {**old, **definition, "updated_at": stamp(now)}
        jobs["jobs"][job_id] = job
        entry = state["jobs"].setdefault(job_id, _initial_state(job, now))
        if definition["every_seconds"] != old.get("every_seconds"):
            entry["next_run"] = stamp(now + timedelta(seconds=definition["every_seconds"]))
        _write_doc(jobs_file(), jobs)
        _write_doc(state_file(), state)
        return _view(job, entry)


def delete_job(job_id: str) -> None:
    """Remove definition and state. ``runs/`` and ``logs/`` are kept. A run in
    progress finishes on its own; its record still lands in ``runs/``."""
    with _lock:
        jobs = _read_doc(jobs_file())
        if job_id not in jobs["jobs"]:
            raise UnknownJobError(job_id)
        del jobs["jobs"][job_id]
        state = _read_doc(state_file())
        state["jobs"].pop(job_id, None)
        _write_doc(jobs_file(), jobs)
        _write_doc(state_file(), state)


def list_runs(job_id: str, limit: int = 20) -> list[dict]:
    """The last ``limit`` run records, newest first."""
    with _lock:
        if job_id not in _read_doc(jobs_file())["jobs"]:
            raise UnknownJobError(job_id)
    try:
        lines = runs_file(job_id).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn line; the rest of the history is still good
    return list(reversed(records[-max(1, int(limit)):]))
