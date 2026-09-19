"""Saved commands, run on a schedule or on demand: the job store and the tick.

Design: docs/superpowers/specs/2026-09-11-command-scheduler-design.md.

An agent registers a job plus an interval and goes away. The jobs module's
supervised task (``tasks.py``) calls :func:`tick` once per watcher tick
interval; the tick launches whatever is due and harvests whatever finished.
It is synchronous, thread-safe and idempotent: state is written to disk
*before* a job is launched, so calling ``tick`` twice with the same ``now``
starts nothing the second time.

Two kinds of job:

* a **command job** ``{"command": {"argv": [...], "timeout": 60, ...}}``
  runs a subprocess through ``utils.commands`` (the executor); its full
  output goes to ``logs/jobs/<id>.log``;
* a **module job** ``{"module": "connections", "command": "poll",
  "args": ["gmail"], "timeout": 60}`` calls another module's command
  (``modules/<name>/commands.py``) in this process. The module and the
  command must exist when the definition is stored; the module's
  ``commands`` switch is checked when the job runs (off: the run fails and
  names Setup). A command that returns an awaitable is awaited on the
  server's event loop when the tick task registered it
  (:func:`attach_loop`), else on a loop of its own. The JSON result is the
  run's output (the first :data:`OUTPUT_TAIL_CHARS` characters).

Both kinds share single-flight, the concurrency cap, the run record and the
history line. Files are ``modules/jobs/store.py``'s: ``jobs.json`` and
``state.json`` are :class:`services.storage.document.Document` (a corrupt
file is refused with a 409 that names the document, never rewritten) and
``runs/<id>.jsonl`` is a :class:`services.storage.eventlog.EventLog`.
Two files, two owners: a re-registration cannot clobber a ``next_run`` and
a tick cannot clobber a definition.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from services import modules as registry
from services.errors import ServiceError
from services.storage.document import Document
from utils.commands import CommandResult, CommandSpec, run_spec_sync
from utils.runtime_env import ENV_WATCHER_INTERVAL, watcher_tick_interval_seconds as tick_interval_seconds

from . import store
from .store import (  # noqa: F401  (re-exported: the paths callers and tests name through this module)
    RUN_EVENT_TYPE,
    jobs_dir,
    jobs_file,
    log_file,
    runs_file,
    state_file,
)

logger = logging.getLogger(__name__)

SCHEMA = store.SCHEMA
ENV_ENABLED = "XO_SCHEDULER_ENABLED"
ENV_MAX_CONCURRENT = "XO_SCHEDULER_MAX_CONCURRENT"
DEFAULT_MAX_CONCURRENT = 4
OUTPUT_TAIL_CHARS = 2000

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
_DEFINITION_KEYS = frozenset(
    {"name", "description", "command", "every_seconds", "first_run_at", "project_id", "enabled",
     "module", "args", "timeout"}
)
#: The keys that say what a job runs; replaced as a set on update so a job
#: that changes kind carries nothing of the other kind along.
_KIND_KEYS = frozenset({"command", "module", "args", "timeout"})


class SchedulerError(ServiceError):
    """A scheduler file cannot be written, or an operation is not valid now
    (500 unless a subclass says otherwise). ``/api/schedules`` has always
    answered these with the bare message as ``detail``, so the family
    carries no code."""

    status = 500

    def __init__(self, message: str) -> None:
        super().__init__(None, message, self.status)


class UnknownJobError(SchedulerError):
    """No job with that id (404)."""

    status = 404

    def __init__(self, job_id: str) -> None:
        super().__init__(f"no such job: {job_id}")
        self.job_id = job_id


class JobRunningError(SchedulerError):
    """The job already has a run in progress (single-flight; 409)."""

    status = 409


class ConcurrencyLimitError(SchedulerError):
    """All command execution slots are occupied (409)."""

    status = 409


class InvalidJobError(SchedulerError, ValueError):
    """A job definition that cannot be stored (400). Still a ``ValueError``
    for callers that validate outside the API."""

    status = 400


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
# Wall-clock, UTC, second granularity: the stamp format the watcher sinks
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
    not drift the schedule. Missed slots collapse: a machine that was off
    for three days gets one run, not three.
    """
    if next_run > now:
        return next_run
    missed = int((now - next_run).total_seconds() // every_seconds) + 1
    return next_run + timedelta(seconds=every_seconds * missed)


# ── Documents ────────────────────────────────────────────────────────────────


def _read(document: Document) -> dict:
    """The document, normalised. Absent is empty; a corrupt file raises the
    Document's own 409 (``corrupt_document``) and is never rewritten."""
    return document.require()


def _write(document: Document, doc: dict) -> None:
    """Replace the whole document (the scheduler owns both entirely): atomic,
    owner-only, stamped. A filesystem failure is a :class:`SchedulerError`
    naming the file, as it always was."""
    try:
        document.write(doc)
    except OSError as exc:
        raise SchedulerError(f"{document.path} could not be written: {exc}") from exc


def _run_line(job_id: str, record: dict) -> dict:
    """One history line: ``ts`` (when the run ended) and ``type`` first, like
    every other event log, then the job id and the run record itself."""
    head = {"ts": record.get("finished_at"), "type": RUN_EVENT_TYPE, "job_id": job_id}
    return {**head, **{k: v for k, v in record.items() if k not in head}}


def _append_run(job_id: str, record: dict) -> None:
    path = runs_file(job_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        store.runs_log(job_id).append([_run_line(job_id, record)])
    except OSError as exc:
        raise SchedulerError(f"{path} could not be appended: {exc}") from exc


# ── Definitions ──────────────────────────────────────────────────────────────


def _validate_module_command(payload: Mapping[str, Any]) -> dict:
    """The module-job half of :func:`validate_definition`: ``module`` and
    ``command`` name a command some module declares right now (400
    otherwise), ``args`` are strings, ``timeout`` is required."""
    module = payload.get("module")
    if not isinstance(module, str) or not module:
        raise InvalidJobError("module must be the name of an installed module")
    command = payload.get("command")
    if not isinstance(command, str) or not command:
        raise InvalidJobError("command must be the name of one of the module's commands when module is given")
    args = payload.get("args")
    if args is None:
        args = []
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise InvalidJobError("args must be a list of strings")
    timeout = payload.get("timeout")
    if timeout is None:
        raise InvalidJobError("timeout is required: a scheduled job without one could run forever")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise InvalidJobError("timeout must be a positive number of seconds")
    available = registry.commands()
    if module not in available:
        raise InvalidJobError(
            f"unknown module {module!r}; modules with commands: {', '.join(sorted(available)) or 'none'}")
    if command not in available[module]:
        raise InvalidJobError(
            f"module {module!r} has no command {command!r}; it has: {', '.join(sorted(available[module])) or 'none'}")
    return {"module": module, "command": command, "args": list(args), "timeout": float(timeout)}


def validate_definition(payload: Any) -> dict:
    """Return a normalised definition (no id, no timestamps) or raise
    :class:`InvalidJobError` (a ``ValueError``).

    Exactly one of the two shapes: a command job (``command`` is an object
    validated by ``CommandSpec.from_json``, the same door the skill catalog
    and manifests use, and stored back as ``argv``, so the tick only ever
    sees a list) or a module job (``module`` plus a command name). A
    ``timeout`` is mandatory in both, even though the executor allows
    ``None``: a job without one could hold its running flag forever.
    """
    if not isinstance(payload, Mapping):
        raise InvalidJobError("job must be a JSON object")
    unknown = set(payload) - _DEFINITION_KEYS
    if unknown:
        raise InvalidJobError(f"unknown job keys: {sorted(unknown)}")
    name = payload.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise InvalidJobError("name must be 1-64 characters: letters, digits, space, '_', '.', '-'")
    description = payload.get("description")
    if description is not None and not isinstance(description, str):
        raise InvalidJobError("description must be a string when given")
    kind: dict[str, Any]
    if "module" in payload:
        kind = _validate_module_command(payload)
    else:
        if "args" in payload or "timeout" in payload:
            raise InvalidJobError(
                "args and timeout belong to a module job (give module); a command job "
                "puts its timeout inside command")
        if "command" not in payload:
            raise InvalidJobError(
                "command is required (a JSON object: argv, timeout, optional cwd/env), "
                "or module plus the name of one of its commands")
        spec = CommandSpec.from_json(payload["command"])  # CommandSpecError: a 400 like InvalidJobError
        if spec.timeout is None:
            raise InvalidJobError("command.timeout is required: a scheduled job without one could run forever")
        command: dict[str, Any] = {"argv": spec.argv, "timeout": spec.timeout}
        if spec.cwd is not None:
            command["cwd"] = spec.cwd
        if spec.env is not None:
            command["env"] = spec.env
        kind = {"command": command}
    every = payload.get("every_seconds")
    if every is not None and (isinstance(every, bool) or not isinstance(every, int) or every < 1):
        raise InvalidJobError("every_seconds must be a positive integer, or null for a job that does not repeat")
    tick = tick_interval_seconds()
    if every is not None and every < tick:
        # The scheduler looks once per tick; a shorter interval is polling,
        # not scheduling, and could not be honoured.
        raise InvalidJobError(
            f"every_seconds={every} is shorter than the watcher tick interval "
            f"({tick:g}s): that is polling, not scheduling. Raise every_seconds or "
            f"lower {ENV_WATCHER_INTERVAL}."
        )
    first_run_at = payload.get("first_run_at")
    if first_run_at is not None:
        # Where the grid starts, or, with every_seconds null, the one instant a
        # one-time job runs. Must carry a UTC offset: the server clock is UTC
        # (Docker), so a naive "19:00" would silently mean the wrong hour.
        # Stored normalised to UTC; a past anchor is fine (see _first_slot).
        if not isinstance(first_run_at, str) or not first_run_at.strip():
            raise InvalidJobError(
                "first_run_at must be an ISO-8601 timestamp string with a UTC offset, "
                "e.g. 2026-09-14T19:00:00+05:30"
            )
        try:
            anchor = datetime.fromisoformat(first_run_at.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidJobError(
                f"first_run_at is not an ISO-8601 timestamp ({first_run_at!r}): "
                f"use e.g. 2026-09-14T19:00:00+05:30"
            ) from exc
        if anchor.tzinfo is None:
            raise InvalidJobError(
                "first_run_at needs a UTC offset (+05:30, or Z): the server clock is "
                "UTC and a naive time would mean the wrong hour"
            )
        first_run_at = stamp(anchor)
    project_id = payload.get("project_id")
    if project_id is not None and (not isinstance(project_id, str) or not project_id):
        raise InvalidJobError("project_id must be a non-empty string when given")
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise InvalidJobError("enabled must be true or false")
    return {
        "name": name,
        "description": description,
        "project_id": project_id,
        **kind,
        "every_seconds": every,
        "first_run_at": first_run_at,
        "enabled": enabled,
    }


def _new_id(name: str, taken: Mapping[str, Any]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32] or "job"
    while True:
        candidate = f"{slug}-{secrets.token_hex(3)}"
        if candidate not in taken:
            return candidate


def _first_slot(job: Mapping[str, Any], now: datetime) -> Optional[datetime]:
    """Where this job's grid starts.

    A job that does not repeat (``every_seconds`` null) is one-time when it
    has ``first_run_at``: due at that instant, so a time already past is due
    at once. Without it, it is manual only and has no slot (``None``).

    A repeating job with ``first_run_at``: that instant if it is still ahead
    (or exactly now), else the first slot strictly after ``now`` on the grid
    it defines, so "every Monday 19:00" can be anchored to *last* Monday and
    still land on the coming one. Without it: one interval from now.
    """
    anchor = job.get("first_run_at")
    if job.get("every_seconds") is None:
        return parse_stamp(anchor) if anchor else None
    every = int(job["every_seconds"])
    if anchor:
        first = parse_stamp(anchor)
        return first if first >= now else advance(first, every, now)
    return now + timedelta(seconds=every)


def _initial_state(job: Mapping[str, Any], now: datetime) -> dict:
    first = _first_slot(job, now)
    return {
        "next_run": stamp(first) if first is not None else None,
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
    history_written: bool = False


_lock = threading.Lock()
_running: dict[str, _Run] = {}
#: The server's event loop, when the tick task registered it: a module
#: job's coroutine runs there, beside its module's own routes.
_loop: Optional[asyncio.AbstractEventLoop] = None


def reset_state() -> None:
    """Forget in-memory runs. For tests."""
    with _lock:
        _running.clear()


def attach_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def detach_loop() -> None:
    global _loop
    _loop = None


def _view(job: Mapping[str, Any], entry: Optional[Mapping[str, Any]]) -> dict:
    """A job as the API shows it: definition merged flat with its state."""
    entry = entry or {"next_run": None, "last_run": None, "running_since": None, "last_result": None}
    return {**job, **entry, "running": job["id"] in _running}


# ── Registration ─────────────────────────────────────────────────────────────


def create_job(payload: Any, *, now: Optional[datetime] = None) -> dict:
    definition = validate_definition(payload)
    now = _resolve_now(now)
    with _lock:
        jobs = _read(store.jobs_document())
        state = _read(store.state_document())
        job_id = _new_id(definition["name"], jobs["jobs"])
        job = {"id": job_id, **definition, "created_at": stamp(now), "updated_at": stamp(now)}
        jobs["jobs"][job_id] = job
        state["jobs"][job_id] = _initial_state(job, now)
        _write(store.jobs_document(), jobs)
        _write(store.state_document(), state)
        return _view(job, state["jobs"][job_id])


def get_job(job_id: str) -> dict:
    with _lock:
        job = _read(store.jobs_document())["jobs"].get(job_id)
        if job is None:
            raise UnknownJobError(job_id)
        return _view(job, _harvest_for_read()["jobs"].get(job_id))


def list_jobs() -> list[dict]:
    with _lock:
        jobs = _read(store.jobs_document())["jobs"]
        state = _harvest_for_read()["jobs"]
        return [_view(jobs[k], state.get(k)) for k in sorted(jobs)]


def update_job(job_id: str, payload: Any, *, now: Optional[datetime] = None) -> dict:
    """Replace the definition, keep the id. A changed interval or anchor
    recomputes ``next_run`` from the new grid; anything else leaves it alone."""
    definition = validate_definition(payload)
    now = _resolve_now(now)
    with _lock:
        jobs = _read(store.jobs_document())
        state = _read(store.state_document())
        old = jobs["jobs"].get(job_id)
        if old is None:
            raise UnknownJobError(job_id)
        kept = {k: v for k, v in old.items() if k not in _KIND_KEYS}
        job = {**kept, **definition, "updated_at": stamp(now)}
        jobs["jobs"][job_id] = job
        entry = state["jobs"].setdefault(job_id, _initial_state(job, now))
        if (
            definition["every_seconds"] != old.get("every_seconds")
            or definition["first_run_at"] != old.get("first_run_at")
        ):
            entry["next_run"] = _initial_state(job, now)["next_run"]
        _write(store.jobs_document(), jobs)
        _write(store.state_document(), state)
        return _view(job, entry)


def delete_job(job_id: str) -> None:
    """Remove definition and state. ``runs/`` and ``logs/`` are kept. A run in
    progress finishes on its own; its record still lands in ``runs/``."""
    with _lock:
        jobs = _read(store.jobs_document())
        if job_id not in jobs["jobs"]:
            raise UnknownJobError(job_id)
        del jobs["jobs"][job_id]
        state = _harvest_for_read()
        state["jobs"].pop(job_id, None)
        _write(store.jobs_document(), jobs)
        _write(store.state_document(), state)


def list_runs(job_id: str, limit: int = 20) -> list[dict]:
    """The last ``limit`` run records, newest first, across the live history
    file and its rotations."""
    with _lock:
        if job_id not in _read(store.jobs_document())["jobs"]:
            raise UnknownJobError(job_id)
        _harvest_for_read()
    return store.runs_log(job_id).tail(limit=max(1, int(limit)))


# ── Launch and harvest ───────────────────────────────────────────────────────


def _failed(argv: list[str], output: str, started: float) -> CommandResult:
    return CommandResult(argv=argv, returncode=1, output=output[:OUTPUT_TAIL_CHARS],
                         duration_seconds=time.monotonic() - started)


async def _awaited(value: Any) -> Any:
    return await value


def _call_command(fn: Any, args: list[str], timeout: float) -> Any:
    """Call a module command the way ``python -m quirq`` does: with the
    arguments as a list; an awaitable result is awaited, on the server's
    loop when one is attached (so the command shares its module's loop-bound
    state with the routes), else on a loop of this thread's own. ``timeout``
    bounds the await; a synchronous command cannot be interrupted."""
    value = fn(list(args))
    if not inspect.isawaitable(value):
        return value
    loop = _loop
    if loop is not None and loop.is_running() and not loop.is_closed():
        future = asyncio.run_coroutine_threadsafe(_awaited(value), loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"timed out after {timeout:g}s") from exc
    try:
        return asyncio.run(asyncio.wait_for(_awaited(value), timeout))
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"timed out after {timeout:g}s") from exc


def _log_module_run(job_id: str, argv: list[str], started_at: datetime,
                    returncode: int, duration: float, text: str) -> None:
    """The run's own log entry, in the executor's shape. Never raises."""
    path = log_file(job_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(f"\n=== {started_at.isoformat()} {job_id} ===\n$ {' '.join(argv)}\n"
                     f"[{returncode}; {duration:.3f}s]\n{text}\n")
    except OSError as exc:
        logger.warning("jobs: could not write %s: %s", path, exc)


def _module_result(job: Mapping[str, Any], started_at: datetime) -> CommandResult:
    """Run a module job to a :class:`CommandResult`: ``ok`` (returncode 0,
    the JSON result as output) or ``failed`` (returncode 1, the reason).
    Never raises."""
    module, command = str(job["module"]), str(job["command"])
    args = [str(a) for a in job.get("args") or []]
    timeout = float(job.get("timeout") or 0) or None
    argv = ["quirq", module, command, *args]
    started = time.monotonic()
    found = registry.find(module)
    title = found.title if found is not None else module
    if not registry.enabled(module, "commands"):
        result = _failed(argv, f"{title} commands are off in Setup, under Modules.", started)
    else:
        fn = (registry.commands().get(module) or {}).get(command)
        if fn is None:
            result = _failed(argv, f"{title} has no command {command!r}", started)
        else:
            try:
                value = _call_command(fn, args, timeout or 0)
            except Exception as exc:  # noqa: BLE001 - one line for a person, as the CLI prints it
                code = getattr(exc, "code", None) or type(exc).__name__
                message = getattr(exc, "message", None) or str(exc)
                result = _failed(argv, f"{code}: {message}", started)
            else:
                text = "" if value is None else json.dumps(value, ensure_ascii=False, default=str)
                result = CommandResult(argv=argv, returncode=0, output=text[:OUTPUT_TAIL_CHARS],
                                       duration_seconds=time.monotonic() - started)
                _log_module_run(str(job["id"]), argv, started_at, 0, result.duration_seconds, text)
                return result
    _log_module_run(str(job["id"]), argv, started_at, result.returncode, result.duration_seconds, result.output)
    return result


def _launch(job: Mapping[str, Any], trigger: str, now: datetime) -> None:
    """Start the job's thread. Lock held; state.json already says it is running.

    A command job's thread calls the executor's synchronous runner, which
    enforces the timeout and never raises; a module job's thread calls the
    command in process (:func:`_module_result`). The try/except is belt and
    braces so a bug in either can never leave a run without a result.
    """
    job_id = str(job["id"])
    run = _Run(job_id=job_id, trigger=trigger, started_at=now)
    if job.get("module"):
        argv = ["quirq", str(job["module"]), str(job["command"])]

        def target() -> None:
            try:
                run.result = _module_result(job, now)
            except Exception as exc:  # noqa: BLE001
                run.result = CommandResult(
                    argv=argv, returncode=-1, output=f"[exception] {exc}",
                    duration_seconds=0.0, exception=str(exc),
                )
            run.finished_at = now_utc()
    else:
        # The log destination is part of the spec (`run_spec_sync` forwards only
        # capture options), so build one spec that carries the command and its log.
        spec = CommandSpec.from_json(
            {**job["command"], "log_path": str(log_file(job_id)), "log_label": job_id}
        )

        def target() -> None:
            try:
                run.result = run_spec_sync(spec)
            except Exception as exc:  # noqa: BLE001
                run.result = CommandResult(
                    argv=spec.argv, returncode=-1, output=f"[exception] {exc}",
                    duration_seconds=0.0, exception=str(exc),
                )
            run.finished_at = now_utc()

    run.thread = threading.Thread(target=target, name=f"jobs:{job_id}", daemon=True)
    _running[job_id] = run
    try:
        run.thread.start()
    except Exception as exc:
        del _running[job_id]
        raise SchedulerError(f"{job_id} could not start its command thread: {exc}") from exc


def _status_of(result: CommandResult) -> str:
    if result.ok:
        return "ok"
    if result.timed_out:
        return "timed_out"
    if result.binary_missing:
        return "missing_binary"
    if result.exception is not None:
        return "error"
    return "failed"


def _record(run: _Run) -> dict:
    result = run.result or CommandResult(argv=[], returncode=-1, output="[no result]",
                                         duration_seconds=0.0, exception="no result")
    return {
        "started_at": stamp(run.started_at),
        "finished_at": stamp(run.finished_at or now_utc()),
        "trigger": run.trigger,
        "status": _status_of(result),
        "returncode": result.returncode,
        "duration_seconds": round(result.duration_seconds, 3),
        "output_tail": result.output[-OUTPUT_TAIL_CHARS:],
    }


def _harvest(state: dict) -> list[str]:
    """Record every run whose thread has ended. Lock held."""
    finished: list[str] = []
    for job_id, run in list(_running.items()):
        if run.thread is None or run.thread.is_alive():
            continue
        record = _record(run)
        if not run.history_written:
            _append_run(job_id, record)
            run.history_written = True
        entry = state["jobs"].get(job_id)
        if entry is not None:  # deleted mid-run: history is kept, state is gone
            entry["last_run"] = record["started_at"]
            entry["last_result"] = record
            entry["running_since"] = None
            # Release the result only after both durable copies exist. If
            # state replacement fails, retry it on the next read/tick without
            # appending the same history row again.
            _write(store.state_document(), state)
        del _running[job_id]
        finished.append(job_id)
    return finished


def _sweep(state: dict, now: datetime) -> list[str]:
    """A job the file says is running but this process is not tracking was
    running when the server stopped. Record it as lost and clear it. Runs
    every tick, so it needs no startup hook. Lock held."""
    lost: list[str] = []
    for job_id, entry in state["jobs"].items():
        if entry.get("running_since") and job_id not in _running:
            record = {
                "started_at": entry["running_since"], "finished_at": stamp(now),
                "trigger": None, "status": "lost", "returncode": None,
                "duration_seconds": None, "output_tail": "",
                "reason": "the server stopped while this run was in progress",
            }
            _append_run(job_id, record)
            entry["last_run"] = record["started_at"]
            entry["running_since"] = None
            entry["last_result"] = record
            lost.append(job_id)
    return lost


def _harvest_for_read() -> dict:
    """Refresh results without launching jobs, including with the tick off.

    Lock held. Reads and run_now share this so a completed run immediately
    frees its slot and every result is persisted before another run starts.
    """
    state = _read(store.state_document())
    finished = _harvest(state)
    lost = _sweep(state, now_utc())
    if finished or lost:
        _write(store.state_document(), state)
    return state


# ── The tick ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TickReport:
    """What one tick did. Ids only, nothing from a job's output."""

    enabled: bool = True
    started: list[str] = field(default_factory=list)
    finished: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not (self.started or self.finished or self.skipped
                    or self.deferred or self.lost or self.errors)

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "started": list(self.started), "finished": list(self.finished),
            "skipped": list(self.skipped), "deferred": list(self.deferred),
            "lost": list(self.lost), "errors": list(self.errors),
        }


def _consider(job: Mapping[str, Any], state: dict, now: datetime, report: TickReport) -> bool:
    """Decide one job. Returns True when ``state`` changed in memory and still
    needs writing (a launch writes the file itself, before the process
    exists, and returns False). Lock held."""
    job_id = str(job["id"])
    once = job.get("every_seconds") is None
    if not job.get("enabled", True) or (once and not job.get("first_run_at")):
        return False  # disabled, or manual only
    entry = state["jobs"].get(job_id)
    if entry is None:
        # Added to jobs.json by hand: adopt it, first run one interval out.
        state["jobs"][job_id] = _initial_state(job, now)
        return True
    if once and not entry.get("next_run"):
        return False  # a one-time job that has run: it waits for Run now or a new time
    try:
        next_run = parse_stamp(entry["next_run"])
    except (KeyError, TypeError, ValueError):
        entry.update(_initial_state(job, now))
        return True
    if now < next_run:
        return False
    if once:
        following = None
        if job_id in _running or len(_running) >= max_concurrent():
            # A one-time run is never skipped, only delayed: next_run stays
            # due until a runner is free.
            report.deferred.append(job_id)
            return False
    else:
        every = int(job["every_seconds"])
        if job_id in _running:
            record = {
                "started_at": stamp(now), "finished_at": stamp(now), "trigger": "schedule",
                "status": "skipped", "returncode": None, "duration_seconds": None,
                "output_tail": "", "reason": "previous run still in progress",
            }
            _append_run(job_id, record)
            entry["last_result"] = record
            entry["next_run"] = stamp(advance(next_run, every, now))
            report.skipped.append(job_id)
            return True
        if len(_running) >= max_concurrent():
            report.deferred.append(job_id)  # next_run untouched: still due next tick
            return False
        following = advance(next_run, every, now)
    entry["running_since"] = stamp(now)
    entry["next_run"] = stamp(following) if following is not None else None
    _write(store.state_document(), state)  # on disk BEFORE the process exists (idempotency)
    try:
        _launch(job, "schedule", now)
    except Exception as exc:  # noqa: BLE001 - a hand-edited definition the executor refuses
        entry["running_since"] = None
        report.errors.append(f"{job_id}: {exc}")
        return True
    report.started.append(job_id)
    if once:
        return False
    tick = tick_interval_seconds()
    if every < tick:
        # Registered under a faster tick, then the watcher was slowed down:
        # the job still runs, once per tick, with its missed slots collapsed.
        # Said once per launch (not per tick) so the log shows it without
        # flooding.
        report.errors.append(
            f"{job_id}: every_seconds={every} is shorter than the watcher tick "
            f"interval ({tick:g}s); running once per tick instead"
        )
    return False


def tick(now: Optional[datetime] = None) -> TickReport:
    """One pass: harvest finished runs, sweep lost ones, launch what is due.

    Synchronous and thread-safe; call it from any thread once per tick. Cheap
    when nothing is due (two small JSON reads). Never raises for a job's
    sake: per-job problems, and a document that cannot be read or written,
    land in ``report.errors``.
    """
    now = _resolve_now(now)
    if not scheduler_enabled():
        return TickReport(enabled=False)
    report = TickReport()
    with _lock:
        try:
            state = _read(store.state_document())
        except ServiceError as exc:
            report.errors.append(exc.message)
            return report
        try:
            report.finished.extend(_harvest(state))
            report.lost.extend(_sweep(state, now))
        except ServiceError as exc:
            report.errors.append(exc.message)
            return report
        changed = bool(report.finished or report.lost)
        try:
            jobs = _read(store.jobs_document())["jobs"]
        except ServiceError as exc:
            report.errors.append(exc.message)
            jobs = {}
        for job_id in sorted(jobs):
            try:
                if _consider(jobs[job_id], state, now, report):
                    changed = True
                elif job_id in report.started:
                    changed = False  # _consider wrote the file; earlier changes went with it
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the others
                report.errors.append(f"{job_id}: {exc}")
        if changed:
            try:
                _write(store.state_document(), state)
            except ServiceError as exc:
                report.errors.append(exc.message)
    return report


# ── Run now ──────────────────────────────────────────────────────────────────


def run_now(job_id: str, *, now: Optional[datetime] = None) -> dict:
    """Start the job immediately (``trigger: manual``). Ignores ``enabled``
    (a manual run is how an agent tests a job before trusting it) and
    does not touch ``next_run``. Single-flight and the shared concurrency cap
    apply to manual runs too."""
    now = _resolve_now(now)
    with _lock:
        job = _read(store.jobs_document())["jobs"].get(job_id)
        if job is None:
            raise UnknownJobError(job_id)
        state = _harvest_for_read()
        if job_id in _running:
            raise JobRunningError(f"{job_id} already has a run in progress")
        if len(_running) >= max_concurrent():
            raise ConcurrencyLimitError(f"command concurrency limit ({max_concurrent()}) reached; try again when a run finishes")
        entry = state["jobs"].setdefault(job_id, _initial_state(job, now))
        entry["running_since"] = stamp(now)
        _write(store.state_document(), state)
        try:
            _launch(job, "manual", now)
        except Exception:
            entry["running_since"] = None
            _write(store.state_document(), state)
            raise
        return _view(job, entry)
