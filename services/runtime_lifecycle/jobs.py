"""Install, update, uninstall and sign-out, each a background job running its
runtime's command from the manifest (never from a request), one at a time per
runtime. A job keeps its events: ``started`` (the version before), one ``log``
per line the command prints, then ``done`` (the version after, and any update
still wanted) or ``error``.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import shutil
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import AsyncIterator

from services.runtime_lifecycle import events
from services.runtime_lifecycle.contract import LifecycleError
from services.runtime_lifecycle.registry import ACTIONS, Runtime, installed
from utils.commands import run, streaming

TIMEOUT_S = 300.0
VERSION_TIMEOUT_S = 30.0
KEEP = 50                       # finished jobs kept for GET .../jobs/{id}
_VERSION = re.compile(r"(\d+\.\d+\.\d+\S*)")
_DOING = {"install": "installed", "update": "updated", "uninstall": "uninstalled", "sign_out": "signed out"}


@dataclass(eq=False)
class Job:
    id: str
    runtime: str
    action: str
    status: str = "running"     # running | done | failed
    events: list[dict] = field(default_factory=list)
    _more: asyncio.Event = field(default_factory=asyncio.Event)

    def emit(self, event: dict) -> None:
        self.events.append(event)
        self._more.set()
        self._more = asyncio.Event()

    def as_dict(self) -> dict:
        return {"id": self.id, "runtime": self.runtime, "action": self.action, "status": self.status,
                "log": [e["line"] for e in self.events if e["type"] == "log"],
                "result": self.events[-1] if self.status != "running" else None}

    async def follow(self, heartbeat_s: float) -> AsyncIterator[dict]:
        """Every event from the first, then each as it comes; ``heartbeat`` while quiet."""
        for seen in itertools.count():
            while seen >= len(self.events):
                if self.status != "running":
                    return
                try:
                    await asyncio.wait_for(self._more.wait(), heartbeat_s)
                except asyncio.TimeoutError:
                    yield {"type": "heartbeat"}
            yield self.events[seen]


_running: dict[str, Job] = {}
_jobs: OrderedDict[str, Job] = OrderedDict()
_tasks: set[asyncio.Task] = set()


def get(runtime: str, job_id: str) -> Job:
    job = _jobs.get(job_id)
    if job is None or job.runtime != runtime:
        raise LifecycleError("unknown_job", f"No job {job_id!r} for {runtime}.", 404)
    return job


def last(runtime: str) -> Job | None:
    """The runtime's latest job, running or finished."""
    return next((job for job in reversed(_jobs.values()) if job.runtime == runtime), None)


async def version_of(binary: str) -> str | None:
    result = await run([binary, "--version"], timeout=VERSION_TIMEOUT_S, log_output=False)
    found = _VERSION.search(result.output) if result.ok else None
    return found.group(1) if found else None


async def start(rt: Runtime, action: str, *, force: bool = False, by: str) -> Job:
    """Start ``action`` on ``rt``; refused when it can't run now (``LifecycleError``)."""
    entry = rt.command(action) if action in ACTIONS else None
    if entry is None:
        raise LifecycleError("unsupported", f"{rt.spec.title} has no {action.replace('_', ' ')} action.", 404)
    if rt.name in _running:
        raise LifecycleError("job_running", f"{rt.package} is already being {_DOING[_running[rt.name].action]}",
                             409, job=_running[rt.name].id)
    missing = [tool for tool in entry.get("needs", ()) if not shutil.which(tool)]
    if missing:
        raise LifecycleError("missing_tool", f"{rt.package} {action} needs {', '.join(missing)} on PATH", 412)
    binary = rt.spec.binary()
    before = None
    if action == "install":
        if installed(binary):
            raise LifecycleError("installed", f"{rt.package} is already installed ({binary})", 409)
    else:
        before = await version_of(binary) if installed(binary) else None
        if before is None:
            raise LifecycleError("cli_missing", f"{rt.package} can't be run ({binary}); install it first", 404)
    if action == "uninstall":
        await _free(rt, force=force, by=by)
    job = Job(uuid.uuid4().hex[:12], rt.name, action)
    _running[rt.name] = _jobs[job.id] = job
    while len(_jobs) > KEEP:
        _jobs.popitem(last=False)
    argv = [binary if part == "{binary}" else part for part in entry["command"]]
    task = asyncio.get_running_loop().create_task(_run(rt, job, argv, before))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


async def _free(rt: Runtime, *, force: bool, by: str) -> None:
    """Refuse to uninstall a runtime sessions are running on, or halt them first with ``force``."""
    from services.cowork_agent.storm import engine as storm_engine

    try:
        engine = storm_engine.current()
    except storm_engine.EngineUnavailable:
        return
    sessions = engine.running(rt.name)
    if sessions and not force:
        raise LifecycleError("runtime_in_use", f"{len(sessions)} session(s) run on {rt.spec.title}; uninstall with "
                             "force to stop them first.", 409, sessions=sessions)
    await asyncio.gather(*(engine.halt(s, reason="runtime_uninstalled", by=by) for s in sessions))


async def _run(rt: Runtime, job: Job, argv: list[str], before: str | None) -> None:
    job.emit({"type": "started", "package": rt.package, "version": before})
    try:
        code = await _stream(job, argv)
        if code != 0:
            raise LifecycleError("failed", f"{rt.package} {job.action.replace('_', ' ')} failed (exit code {code})",
                                 502)
        done: dict = {"type": "done", "returncode": 0}
        if job.action in ("install", "update"):
            after = await version_of(rt.spec.binary())
            done.update(version_before=before, version=after, update=rt.spec.update_note(after))
        job.status = "done"
        job.emit(done)
    except LifecycleError as exc:
        job.status = "failed"
        job.emit({"type": "error", "code": exc.code, "error": exc.message, "message": exc.message})
    except Exception as exc:  # noqa: BLE001 - the job says it failed rather than hanging as running
        job.status = "failed"
        job.emit({"type": "error", "code": "failed", "error": str(exc), "message": str(exc)})
    finally:
        _running.pop(rt.name, None)
        await events.changed(rt.name)


async def _stream(job: Job, argv: list[str]) -> int:
    """Run ``argv``, each line it prints (out or err) a ``log`` event; its exit code."""
    lines: asyncio.Queue[str | None] = asyncio.Queue()

    async def pump(stream: asyncio.StreamReader | None) -> None:
        try:
            while stream is not None and (raw := await stream.readline()):
                if text := raw.decode("utf-8", errors="replace").rstrip():
                    await lines.put(text)
        finally:
            await lines.put(None)

    env = {**os.environ, "BROWSER": "true", "DISPLAY": ""}     # never open a browser on the server
    timeout = LifecycleError("timeout", f"{argv[0]} didn't finish within {TIMEOUT_S:g} s", 504)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT_S
    async with streaming(argv, env=env) as proc:
        pumps = [loop.create_task(pump(proc.stdout)), loop.create_task(pump(proc.stderr))]
        try:
            open_streams = len(pumps)
            while open_streams:
                try:
                    line = await asyncio.wait_for(lines.get(), deadline - loop.time())
                except asyncio.TimeoutError:
                    raise timeout from None
                if line is None:
                    open_streams -= 1
                else:
                    job.emit({"type": "log", "line": line})
            try:
                return await asyncio.wait_for(proc.wait(), max(deadline - loop.time(), 1.0))
            except asyncio.TimeoutError:
                raise timeout from None
        finally:
            for task in pumps:
                task.cancel()
