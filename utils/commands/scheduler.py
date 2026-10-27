"""Manual and fixed-interval commands: the job store and the tick.

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
                     written by registration, tick, run_now and read-time harvest
    runs/<id>.jsonl  append-only run history, newest last
and, under ``<quirq state>/logs/scheduler/`` with the other logs:
    <id>.log         the executor's own log of every run (full output,
                     credentials redacted; never capped or rotated — that
                     only happens to the shared commands.log)

Two files, two owners: a re-registration cannot clobber a ``next_run`` and a
tick cannot clobber a definition. The scheduler never imports the watcher
and never imports the visualizer package; it only takes a timestamp.

Two kinds of job (``kind``, absent meaning ``command``, so a command job's
definition and view are exactly what they were before agent jobs). A
``command`` runs its ``CommandSpec`` through the executor. An ``agent`` job
(storm design ch 10 §4.5, ch 12 §9) holds a project chat, a prompt and
optionally the agent, model and effort its runs use; each fire opens a
thread in that chat. ``utils/`` must not import ``services/``, so storm
registers how a fire is run (``set_agent_runner``) at its boot. The runner
hands the fire to storm's event loop and returns a future at once: the tick
never waits for a thread to open. The fire's time is recorded in
``state.json`` before it is handed over, and storm opens at most one thread
per ``(job, fire time)``, so a duplicate tick, a manual run in the same
second and a restart that hands the same fire over again open one thread.

What applies to agent runs: single-flight, while a fire is being opened (a
run's job is to open its thread; the thread then works on its own, so two
runs' threads may work at once). Not the concurrency cap, which counts
command processes: an agent run holds no executor slot, and storm's engine
admits the thread's process. Until storm has registered its runner (it boots
after the watcher's first tick), a due agent job waits, like a job held by
the cap, and a manual run is refused (``AgentJobsUnavailable``).

This is the scheduling half of the command utility (the executor is the
package ``__init__``). ``utils/`` sits below ``services/`` and must not import
it; the two environment facts it shares with the services layer — the Quirq
state root and the watcher's tick interval — come from ``utils/runtime_env.py``,
which is also what ``local_state`` and the watcher use. One definition each.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from utils.commands import CommandResult, CommandSpec, run_spec_sync
from utils.runtime_env import (
    ENV_WATCHER_INTERVAL,
    logs_dir,
    scheduler_dir,
    watcher_tick_interval_seconds as tick_interval_seconds,
)
from utils.safe_read import read_text_guarded

logger = logging.getLogger(__name__)

SCHEMA = 1
ENV_ENABLED = "XO_SCHEDULER_ENABLED"
ENV_MAX_CONCURRENT = "XO_SCHEDULER_MAX_CONCURRENT"
DEFAULT_MAX_CONCURRENT = 4
OUTPUT_TAIL_CHARS = 2000

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
#: A job's name; an agent job's title (its threads are named after it).
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$"
NAME_MAX = 64
_NAME_RE = re.compile(NAME_PATTERN)
KIND_COMMAND = "command"
KIND_AGENT = "agent"
_DEFINITION_KEYS = frozenset(
    {"kind", "name", "description", "command", "every_seconds", "first_run_at", "project_id", "enabled"}
)
_AGENT_KEYS = frozenset(
    {"kind", "name", "description", "chat", "prompt", "agent", "model", "effort", "every_seconds", "first_run_at",
     "enabled"}
)
#: An agent job's prompt: what each run is asked, as the owner's words.
PROMPT_MAX = 20_000
#: An agent job's chat id, agent, model or effort.
FIELD_MAX = 200
#: Each kind's own keys: a change of kind drops the old kind's.
_COMMAND_ONLY = frozenset({"command", "project_id"})
_AGENT_ONLY = frozenset({"kind", "chat", "prompt", "agent", "model", "effort", "utc_offset", "created_by",
                         "approved_by"})
PERSON = {"kind": "person"}
#: What an agent run's record keeps of the runner's outcome.
_OUTCOME_KEYS = ("chat_id", "thread_ts", "already", "code", "reason")
#: The statuses an agent run's record takes from the runner's outcome as they are; any other is ``failed``.
_AGENT_STATUSES = ("ok", "skipped")


class SchedulerError(Exception):
    """A scheduler file cannot be read, or an operation is not valid now."""


class UnknownJobError(SchedulerError):
    """No job with that id."""


class JobRunningError(SchedulerError):
    """The job already has a run in progress (single-flight)."""


class ConcurrencyLimitError(SchedulerError):
    """All command execution slots are occupied."""


class AgentJobsUnavailable(SchedulerError):
    """No agent runner is registered: storm isn't running in this server."""


# ── Paths ────────────────────────────────────────────────────────────────────


def jobs_file() -> Path:
    return scheduler_dir() / "jobs.json"


def state_file() -> Path:
    return scheduler_dir() / "state.json"


def runs_file(job_id: str) -> Path:
    return scheduler_dir() / "runs" / f"{job_id}.jsonl"


def log_file(job_id: str) -> Path:
    # Logs are safe to delete, so they sit with the other logs, not the history.
    return logs_dir() / "scheduler" / f"{job_id}.log"


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
        text = read_text_guarded(path)
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
    if shape_problem(doc) is not None:
        raise SchedulerError(f"{path} must be a JSON object with a 'jobs' object")
    return doc


def shape_problem(doc: Any) -> Optional[str]:
    """Why ``jobs.json`` or ``state.json`` is refused, or ``None``. Pure, so
    xo-doctor can ask the scheduler's own question."""
    if not isinstance(doc, dict):
        return f"the document is a {type(doc).__name__}, expected object"
    jobs = doc.get("jobs")
    if jobs is None:
        return "no jobs object"
    if not isinstance(jobs, dict):
        return f"jobs is a {type(jobs).__name__}, expected object"
    return None


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows, or a filesystem without modes


def _write_doc(path: Path, doc: dict) -> None:
    """Atomic replace. Deliberately not the visualizer's ``write_json_atomic``:
    the scheduler must not depend on the watcher's package — the dependency
    points the other way (the watcher calls us)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _chmod_private(tmp)
        os.replace(tmp, path)
    except OSError as exc:
        raise SchedulerError(f"{path} could not be written: {exc}") from exc


#: The ``type`` of every line in ``runs/<id>.jsonl``; ``status`` is the outcome.
RUN_EVENT_TYPE = "job.run"


def _run_line(job_id: str, record: dict) -> dict:
    """One history line: ``ts`` (when the run ended) and ``type`` first, like
    every other event log, then the job id and the run record itself."""
    head = {"ts": record.get("finished_at"), "type": RUN_EVENT_TYPE, "job_id": job_id}
    return {**head, **{k: v for k, v in record.items() if k not in head}}


def _append_run(job_id: str, record: dict) -> None:
    path = runs_file(job_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(_run_line(job_id, record), ensure_ascii=False) + "\n")
        _chmod_private(path)
    except OSError as exc:
        raise SchedulerError(f"{path} could not be appended: {exc}") from exc


# ── Definitions ──────────────────────────────────────────────────────────────


def validate_definition(payload: Any) -> dict:
    """Return a normalised definition (no id, no timestamps) or raise ValueError.

    The command is validated by ``CommandSpec.from_json`` — the same door the
    skill catalog and manifests use — and stored back as ``argv``, so the
    tick only ever sees a list. ``timeout`` is mandatory here even though the
    executor allows ``None``: a job without one could hold its running flag
    forever.

    An agent job (``kind: "agent"``) is checked for its shape only: whether
    its chat exists and its agent, model and effort are real is storm's to
    say (it checks them before it saves one, and again at each fire).
    """
    if not isinstance(payload, Mapping):
        raise ValueError("job must be a JSON object")
    kind = payload.get("kind", KIND_COMMAND)
    if kind not in (KIND_COMMAND, KIND_AGENT):
        raise ValueError(f'kind must be "{KIND_COMMAND}" or "{KIND_AGENT}"')
    if kind == KIND_AGENT:
        return _validate_agent(payload)
    unknown = set(payload) - _DEFINITION_KEYS
    if unknown:
        raise ValueError(f"unknown job keys: {sorted(unknown)}")
    name, description = _name(payload), _description(payload)
    if "command" not in payload:
        raise ValueError("command is required (a JSON object: argv, timeout, optional cwd/env)")
    spec = CommandSpec.from_json(payload["command"])  # CommandSpecError is a ValueError
    if spec.timeout is None:
        raise ValueError("command.timeout is required: a scheduled job without one could run forever")
    every = _every(payload)
    first_run_at, _offset = _first_run_at(payload)
    project_id = payload.get("project_id")
    if project_id is not None and (not isinstance(project_id, str) or not project_id):
        raise ValueError("project_id must be a non-empty string when given")
    enabled = _enabled(payload)
    command: dict[str, Any] = {"argv": spec.argv, "timeout": spec.timeout}
    if spec.cwd is not None:
        command["cwd"] = spec.cwd
    if spec.env is not None:
        command["env"] = spec.env
    return {
        "name": name,
        "description": description,
        "project_id": project_id,
        "command": command,
        "every_seconds": every,
        "first_run_at": first_run_at,
        "enabled": enabled,
    }


def _validate_agent(payload: Mapping[str, Any]) -> dict:
    unknown = set(payload) - _AGENT_KEYS
    if unknown:
        raise ValueError(f"unknown job keys: {sorted(unknown)}")
    name, description = _name(payload), _description(payload)
    chat = payload.get("chat")
    if not isinstance(chat, str) or not chat.strip() or len(chat) > FIELD_MAX:
        raise ValueError("chat is required: the id of the project chat whose threads run this job "
                         "(GET /api/chat/chats lists them)")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required: what each run asks the agent, in the owner's words")
    if len(prompt) > PROMPT_MAX:
        raise ValueError(f"prompt may hold at most {PROMPT_MAX} characters")
    chosen = {}
    for key in ("agent", "model", "effort"):
        value = payload.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > FIELD_MAX):
            raise ValueError(f"{key} must be a non-empty string of at most {FIELD_MAX} characters when given")
        chosen[key] = value
    every = _every(payload)
    first_run_at, offset = _first_run_at(payload)
    return {
        "kind": KIND_AGENT,
        "name": name,
        "description": description,
        "chat": chat,
        "prompt": prompt,
        **chosen,
        "every_seconds": every,
        "first_run_at": first_run_at,
        # The offset first_run_at was given in, so a run's time reads as the owner wrote it.
        "utc_offset": offset,
        "enabled": _enabled(payload),
    }


def _name(payload: Mapping[str, Any]) -> str:
    name = payload.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError("name must be 1-64 characters: letters, digits, space, '_', '.', '-'")
    return name


def _description(payload: Mapping[str, Any]) -> Optional[str]:
    description = payload.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("description must be a string when given")
    return description


def min_every_seconds() -> int:
    """The shortest interval a job may repeat at: the watcher's tick, in whole seconds."""
    return max(1, math.ceil(tick_interval_seconds()))


def _every(payload: Mapping[str, Any]) -> Optional[int]:
    every = payload.get("every_seconds")
    if every is not None and (isinstance(every, bool) or not isinstance(every, int) or every < 1):
        raise ValueError("every_seconds must be a positive integer, or null for a job that does not repeat")
    tick = tick_interval_seconds()
    if every is not None and every < tick:
        # The scheduler looks once per tick; a shorter interval is polling,
        # not scheduling, and could not be honoured.
        raise ValueError(
            f"every_seconds={every} is shorter than the watcher tick interval "
            f"({tick:g}s): that is polling, not scheduling. Raise every_seconds or "
            f"lower {ENV_WATCHER_INTERVAL}."
        )
    return every


def _first_run_at(payload: Mapping[str, Any]) -> tuple[Optional[str], str]:
    """The anchor as a UTC stamp, and the UTC offset it was given in."""
    first_run_at = payload.get("first_run_at")
    if first_run_at is None:
        return None, "+00:00"
    # Where the grid starts, or, with every_seconds null, the one instant a
    # one-time job runs. Must carry a UTC offset: the server clock is UTC
    # (Docker), so a naive "19:00" would silently mean the wrong hour.
    # Stored normalised to UTC; a past anchor is fine (see _first_slot).
    if not isinstance(first_run_at, str) or not first_run_at.strip():
        raise ValueError(
            "first_run_at must be an ISO-8601 timestamp string with a UTC offset, "
            "e.g. 2026-09-14T19:00:00+05:30"
        )
    try:
        anchor = datetime.fromisoformat(first_run_at.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"first_run_at is not an ISO-8601 timestamp ({first_run_at!r}): "
            f"use e.g. 2026-09-14T19:00:00+05:30"
        ) from exc
    if anchor.tzinfo is None:
        raise ValueError(
            "first_run_at needs a UTC offset (+05:30, or Z): the server clock is "
            "UTC and a naive time would mean the wrong hour"
        )
    return stamp(anchor), _offset_text(anchor.utcoffset() or timedelta(0))


def _offset_text(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def _enabled(payload: Mapping[str, Any]) -> bool:
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    return enabled


def is_agent(job: Mapping[str, Any]) -> bool:
    return job.get("kind") == KIND_AGENT


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
    it defines — so "every Monday 19:00" can be anchored to *last* Monday and
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
    future: Optional[Future] = None          # an agent run: its fire being opened by storm
    fire_at: Optional[str] = None

    def ended(self) -> bool:
        if self.future is not None:
            return self.future.done()
        return self.thread is not None and not self.thread.is_alive()


#: How an agent job's fire is run: given the job, the fire's time and its
#: trigger, it hands the fire to storm and returns a future of the outcome
#: (``{"status": "ok", "chat_id", "thread_ts", "already"?}``,
#: ``{"status": "skipped", "reason"}`` while the job's previous run is still
#: working, or ``{"status": "failed", "code", "reason"}``) without waiting for it.
AgentRunner = Callable[[Mapping[str, Any], str, str], Future]

_lock = threading.Lock()
_running: dict[str, _Run] = {}
_agent_runner: Optional[AgentRunner] = None


def reset_state() -> None:
    """Forget in-memory runs and the agent runner. For tests."""
    global _agent_runner
    with _lock:
        _running.clear()
        _agent_runner = None


def set_agent_runner(runner: AgentRunner) -> None:
    """Storm's boot: how this server runs agent jobs from now on."""
    global _agent_runner
    with _lock:
        _agent_runner = runner


def clear_agent_runner(runner: AgentRunner) -> None:
    """Storm's shutdown: agent jobs wait again (a runner registered since stays)."""
    global _agent_runner
    with _lock:
        if _agent_runner is runner:
            _agent_runner = None


def _commands_running() -> int:
    """The executor slots in use: what the concurrency cap counts."""
    return sum(1 for run in _running.values() if run.future is None)


def _view(job: Mapping[str, Any], entry: Optional[Mapping[str, Any]]) -> dict:
    """A job as the API shows it: definition merged flat with its state."""
    entry = entry or {"next_run": None, "last_run": None, "running_since": None, "last_result": None}
    return {**job, **entry, "running": job["id"] in _running}


# ── Registration ─────────────────────────────────────────────────────────────


def create_job(payload: Any, *, now: Optional[datetime] = None, created_by: Optional[Mapping[str, Any]] = None,
               approved_by: Optional[Mapping[str, Any]] = None) -> dict:
    """Register a job. An agent job records who made it (``created_by``,
    the person by default) and, when an agent asked for it, the review item
    the person allowed (``approved_by: {review_id}``): registering the same
    item again returns the job it made, so a registration retried after a
    restart never makes two."""
    definition = validate_definition(payload)
    if is_agent(definition):
        definition["created_by"] = dict(created_by or PERSON)
        definition["approved_by"] = _approval(approved_by)
    elif created_by is not None or approved_by is not None:
        raise ValueError("created_by and approved_by belong to agent jobs")
    now = _resolve_now(now)
    with _lock:
        jobs = _read_doc(jobs_file())
        state = _read_doc(state_file())
        made = _made_by(jobs["jobs"], definition.get("approved_by"))
        if made is not None:
            return _view(made, state["jobs"].get(made["id"]))
        job_id = _new_id(definition["name"], jobs["jobs"])
        job = {"id": job_id, **definition, "created_at": stamp(now), "updated_at": stamp(now)}
        jobs["jobs"][job_id] = job
        state["jobs"][job_id] = _initial_state(job, now)
        _write_doc(jobs_file(), jobs)
        _write_doc(state_file(), state)
        return _view(job, state["jobs"][job_id])


def _approval(approved_by: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if approved_by is None:
        return None
    review_id = approved_by.get("review_id")
    if set(approved_by) != {"review_id"} or not isinstance(review_id, str) or not review_id:
        raise ValueError("approved_by is {review_id}: the review item the person allowed")
    return {"review_id": review_id}


def _made_by(jobs: Mapping[str, Any], approval: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if approval is None:
        return None
    return next((job for job in jobs.values()
                 if (job.get("approved_by") or {}).get("review_id") == approval["review_id"]), None)


def get_job(job_id: str) -> dict:
    with _lock:
        job = _read_doc(jobs_file())["jobs"].get(job_id)
        if job is None:
            raise UnknownJobError(job_id)
        return _view(job, _harvest_for_read()["jobs"].get(job_id))


def list_jobs() -> list[dict]:
    with _lock:
        jobs = _read_doc(jobs_file())["jobs"]
        state = _harvest_for_read()["jobs"]
        return [_view(jobs[k], state.get(k)) for k in sorted(jobs)]


def update_job(job_id: str, payload: Any, *, now: Optional[datetime] = None) -> dict:
    """Replace the definition, keep the id. A changed interval or anchor
    recomputes ``next_run`` from the new grid; anything else leaves it alone."""
    definition = validate_definition(payload)
    now = _resolve_now(now)
    with _lock:
        jobs = _read_doc(jobs_file())
        state = _read_doc(state_file())
        old = jobs["jobs"].get(job_id)
        if old is None:
            raise UnknownJobError(job_id)
        dropped = _dropped_keys(old, definition)
        job = {**{k: v for k, v in old.items() if k not in dropped}, **definition, "updated_at": stamp(now)}
        if is_agent(job):
            job.setdefault("created_by", dict(PERSON))
            job.setdefault("approved_by", None)
            if job["prompt"] != old.get("prompt"):
                job["approved_by"] = None       # the person's own words now, not what they allowed
        jobs["jobs"][job_id] = job
        entry = state["jobs"].setdefault(job_id, _initial_state(job, now))
        if not is_agent(job):
            entry.pop("fire", None)     # a fire in flight belongs to the agent job it was
        if (
            definition["every_seconds"] != old.get("every_seconds")
            or definition["first_run_at"] != old.get("first_run_at")
        ):
            entry["next_run"] = _initial_state(job, now)["next_run"]
        _write_doc(jobs_file(), jobs)
        _write_doc(state_file(), state)
        return _view(job, entry)


def _dropped_keys(old: Mapping[str, Any], definition: Mapping[str, Any]) -> set[str]:
    """What a replaced definition loses of the old one: a disabled record once
    it is enabled again, and, when the kind changes, the old kind's own keys
    (who made an agent job goes with it). Everything else of the old
    definition stays, exactly as a command job's update always kept it."""
    dropped = {"disabled_by"} if definition["enabled"] else set()
    if is_agent(old) and not is_agent(definition):
        dropped |= _AGENT_ONLY
    elif is_agent(definition) and not is_agent(old):
        dropped |= _COMMAND_ONLY
    return dropped


def disable_job(job_id: str, *, by: Mapping[str, Any], now: Optional[datetime] = None,
                chat: Optional[str] = None) -> dict:
    """Disable the job at once, recording who did it (``disabled_by``).
    Already disabled: unchanged, the first record kept. With ``chat``, only
    an agent job still in that chat (a caller that reaches only one chat's
    jobs checked it earlier): one moved since is unknown to that caller."""
    now = _resolve_now(now)
    with _lock:
        jobs = _read_doc(jobs_file())
        job = jobs["jobs"].get(job_id)
        if job is None or (chat is not None and job.get("chat") != chat):
            raise UnknownJobError(job_id)
        if job.get("enabled", True):
            job = {**job, "enabled": False, "disabled_by": {**by, "at": stamp(now)}, "updated_at": stamp(now)}
            jobs["jobs"][job_id] = job
            _write_doc(jobs_file(), jobs)
        return _view(job, _harvest_for_read()["jobs"].get(job_id))


def delete_job(job_id: str) -> None:
    """Remove definition and state. ``runs/`` and ``logs/`` are kept. A run in
    progress finishes on its own; its record still lands in ``runs/``."""
    with _lock:
        jobs = _read_doc(jobs_file())
        if job_id not in jobs["jobs"]:
            raise UnknownJobError(job_id)
        del jobs["jobs"][job_id]
        state = _harvest_for_read()
        state["jobs"].pop(job_id, None)
        _write_doc(jobs_file(), jobs)
        _write_doc(state_file(), state)


def list_runs(job_id: str, limit: int = 20) -> list[dict]:
    """The last ``limit`` run records, newest first."""
    with _lock:
        if job_id not in _read_doc(jobs_file())["jobs"]:
            raise UnknownJobError(job_id)
        _harvest_for_read()
    try:
        lines = read_text_guarded(runs_file(job_id), max_bytes=None).splitlines()
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


# ── Launch and harvest ───────────────────────────────────────────────────────


def _launch(job: Mapping[str, Any], trigger: str, now: datetime) -> None:
    """Start the job's thread. Lock held; state.json already says it is running.

    The thread calls the executor's synchronous runner, which enforces the
    timeout and never raises; the try/except is belt and braces so a bug in
    the runner can never leave a run without a result.
    """
    job_id = str(job["id"])
    # The log destination is part of the spec (`run_spec_sync` forwards only
    # capture options), so build one spec that carries the command and its log.
    spec = CommandSpec.from_json(
        {**job["command"], "log_path": str(log_file(job_id)), "log_label": job_id}
    )
    run = _Run(job_id=job_id, trigger=trigger, started_at=now)

    def target() -> None:
        try:
            run.result = run_spec_sync(spec)
        except Exception as exc:  # noqa: BLE001
            run.result = CommandResult(
                argv=spec.argv, returncode=-1, output=f"[exception] {exc}",
                duration_seconds=0.0, exception=str(exc),
            )
        run.finished_at = now_utc()

    run.thread = threading.Thread(target=target, name=f"scheduler:{job_id}", daemon=True)
    _running[job_id] = run
    try:
        run.thread.start()
    except Exception as exc:
        del _running[job_id]
        raise SchedulerError(f"{job_id} could not start its command thread: {exc}") from exc


def _dispatch(job: Mapping[str, Any], fire: Mapping[str, str], now: datetime) -> None:
    """Hand an agent job's fire to storm. Lock held; state.json already holds
    the fire (``entry["fire"]``), so a restart hands it over again."""
    job_id = str(job["id"])
    runner = _agent_runner
    if runner is None:
        raise AgentJobsUnavailable("agent jobs run on storm, which isn't running in this server yet; try again once "
                                   "it has started")
    run = _Run(job_id=job_id, trigger=fire["trigger"], started_at=now, fire_at=fire["at"])
    try:
        run.future = runner(dict(job), fire["at"], fire["trigger"])
    except Exception as exc:  # noqa: BLE001 - the loop is gone (the server is stopping)
        raise SchedulerError(f"{job_id} could not hand its run to storm: {exc}") from exc
    _running[job_id] = run


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
    if run.future is not None:
        return _agent_record(run)
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


def _agent_record(run: _Run) -> dict:
    """An agent run's history line: the thread its fire opened, or why it
    couldn't."""
    try:
        outcome = run.future.result()
    except Exception as exc:  # noqa: BLE001 - storm's runner answers with an outcome; anything else is a bug there
        outcome = {"status": "failed", "code": "error", "reason": f"the run raised {type(exc).__name__}"}
    if not isinstance(outcome, Mapping):
        outcome = {"status": "failed", "code": "error", "reason": "the run gave no outcome"}
    return {
        "started_at": stamp(run.started_at), "finished_at": stamp(now_utc()), "trigger": run.trigger,
        "fire_at": run.fire_at, "status": outcome["status"] if outcome.get("status") in _AGENT_STATUSES else "failed",
        **{k: outcome[k] for k in _OUTCOME_KEYS if outcome.get(k) is not None},
    }


def _harvest(state: dict) -> list[str]:
    """Record every run that has ended. Lock held. An agent run cancelled
    before its thread opened (the server stopped) records nothing: its fire
    stays in state.json and is handed over again. A server that stops between
    a fire's history line and clearing it from state.json hands it over again
    too: storm finds the thread it opened, and the fire gets a second line
    saying so (``already``), never a second thread."""
    finished: list[str] = []
    for job_id, run in list(_running.items()):
        if not run.ended():
            continue
        if run.future is not None and run.future.cancelled():
            del _running[job_id]
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
            entry.pop("fire", None)
            # Release the result only after both durable copies exist. If
            # state replacement fails, retry it on the next read/tick without
            # appending the same history row again.
            _write_doc(state_file(), state)
        del _running[job_id]
        finished.append(job_id)
    return finished


def _sweep(state: dict, now: datetime) -> list[str]:
    """A job the file says is running but this process is not tracking was
    running when the server stopped. Record it as lost and clear it. Runs
    every tick, so it needs no startup hook. Lock held."""
    lost: list[str] = []
    for job_id, entry in state["jobs"].items():
        # An agent fire in flight is not lost: the tick hands it over again.
        if entry.get("running_since") and job_id not in _running and not entry.get("fire"):
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
    """Refresh results without launching jobs, including with the watcher off.

    Lock held. Reads and run_now share this so a completed run immediately
    frees its slot and every result is persisted before another run starts.
    """
    state = _read_doc(state_file())
    finished = _harvest(state)
    lost = _sweep(state, now_utc())
    if finished or lost:
        _write_doc(state_file(), state)
    return state


# ── The tick ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TickReport:
    """What one tick did. Ids only — nothing from a job's output."""

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
    agent = is_agent(job)
    if agent and job_id not in _running and (state["jobs"].get(job_id) or {}).get("fire"):
        return _hand_over_again(job, state, now, report)
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
        fire_at = next_run
        if job_id in _running or _held(agent):
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
        if _held(agent):
            report.deferred.append(job_id)  # next_run untouched: still due next tick
            return False
        following = advance(next_run, every, now)
        # Missed slots collapse into one run: the latest slot that is due.
        fire_at = following - timedelta(seconds=every)
    entry["running_since"] = stamp(now)
    entry["next_run"] = stamp(following) if following is not None else None
    if agent:
        entry["fire"] = {"at": stamp(fire_at), "trigger": "schedule"}
    _write_doc(state_file(), state)  # on disk BEFORE the process exists (idempotency)
    try:
        if agent:
            _dispatch(job, entry["fire"], now)
        else:
            _launch(job, "schedule", now)
    except Exception as exc:  # noqa: BLE001 — a hand-edited definition the executor refuses
        report.errors.append(f"{job_id}: {exc}")
        if agent:
            # Storm's loop refused it (the server is stopping): the fire stays
            # in state.json and is handed over again, never lost.
            return False
        entry["running_since"] = None
        return True
    report.started.append(job_id)
    if once:
        return False
    tick = tick_interval_seconds()
    if every < tick:
        # Registered under a faster tick, then the watcher was slowed down:
        # the job still runs, once per tick, with its missed slots collapsed.
        # Said once per launch (not per tick) so the heartbeat shows it
        # without flooding the log.
        report.errors.append(
            f"{job_id}: every_seconds={every} is shorter than the watcher tick "
            f"interval ({tick:g}s); running once per tick instead"
        )
    return False


def _held(agent: bool) -> bool:
    """Whether a due job of this kind waits: a command for a free executor
    slot, an agent job for storm's runner."""
    return _agent_runner is None if agent else _commands_running() >= max_concurrent()


def _hand_over_again(job: Mapping[str, Any], state: dict, now: datetime, report: TickReport) -> bool:
    """A fire state.json holds but no run here is opening: the server stopped
    before it was harvested. Storm opens one thread per fire, so handing it
    over again opens it if it never was and finds it if it was. A fire is
    honoured even if its job was disabled since: it was due before that."""
    job_id = str(job["id"])
    entry = state["jobs"][job_id]
    if _agent_runner is None:
        report.deferred.append(job_id)
        return False
    _write_doc(state_file(), state)  # like any launch: what changed earlier this tick goes to disk with it
    try:
        _dispatch(job, entry["fire"], now)
    except SchedulerError as exc:
        report.errors.append(f"{job_id}: {exc}")
        return False
    report.started.append(job_id)
    return False


def tick(now: Optional[datetime] = None) -> TickReport:
    """One pass: harvest finished runs, sweep lost ones, launch what is due.

    Synchronous and thread-safe; call it from any thread once per tick. Cheap
    when nothing is due (two small JSON reads). Never raises for a job's
    sake: per-job problems land in ``report.errors``.
    """
    now = _resolve_now(now)
    if not scheduler_enabled():
        return TickReport(enabled=False)
    report = TickReport()
    with _lock:
        try:
            state = _read_doc(state_file())
        except SchedulerError as exc:
            report.errors.append(str(exc))
            return report
        try:
            report.finished.extend(_harvest(state))
            report.lost.extend(_sweep(state, now))
        except SchedulerError as exc:
            report.errors.append(str(exc))
            return report
        changed = bool(report.finished or report.lost)
        try:
            jobs = _read_doc(jobs_file())["jobs"]
        except SchedulerError as exc:
            report.errors.append(str(exc))
            jobs = {}
        for job_id in sorted(jobs):
            try:
                if _consider(jobs[job_id], state, now, report):
                    changed = True
                elif job_id in report.started:
                    changed = False  # _consider wrote the file; earlier changes went with it
            except Exception as exc:  # noqa: BLE001 — one bad job must not stop the others
                report.errors.append(f"{job_id}: {exc}")
        if changed:
            _write_doc(state_file(), state)
    return report


# ── Run now ──────────────────────────────────────────────────────────────────


def run_now(job_id: str, *, now: Optional[datetime] = None) -> dict:
    """Start the job immediately (``trigger: manual``). Ignores ``enabled`` —
    a manual run is how an agent tests a job before trusting it — and
    does not touch ``next_run``. Single-flight and the shared concurrency cap
    apply to manual runs too (the cap to commands only: see the module's
    docstring). An agent job's manual fire is this second."""
    now = _resolve_now(now)
    with _lock:
        job = _read_doc(jobs_file())["jobs"].get(job_id)
        if job is None:
            raise UnknownJobError(job_id)
        state = _harvest_for_read()
        agent = is_agent(job)
        if agent and _agent_runner is None:
            raise AgentJobsUnavailable("agent jobs run on storm, which isn't running in this server yet; try again "
                                       "once it has started")
        if job_id in _running or (agent and (state["jobs"].get(job_id) or {}).get("fire")):
            raise JobRunningError(f"{job_id} already has a run in progress")
        if not agent and _commands_running() >= max_concurrent():
            raise ConcurrencyLimitError(f"command concurrency limit ({max_concurrent()}) reached; try again when a run finishes")
        entry = state["jobs"].setdefault(job_id, _initial_state(job, now))
        entry["running_since"] = stamp(now)
        if agent:
            entry["fire"] = {"at": stamp(now), "trigger": "manual"}
        _write_doc(state_file(), state)
        try:
            if agent:
                _dispatch(job, entry["fire"], now)
            else:
                _launch(job, "manual", now)
        except Exception:
            entry["running_since"] = None
            entry.pop("fire", None)
            _write_doc(state_file(), state)
            raise
        return _view(job, entry)
