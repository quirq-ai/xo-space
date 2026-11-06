"""Deploy runs: the launchpad script as a server-side process.

A deploy is one run of ``launchpad.py deploy``, started through
``utils.commands.spawn_stream`` (the subprocess→SSE pattern of
``routers/auth/claude_setup_token.py``): its JSON events are kept in order
and handed to every viewer as server-sent events. The process belongs to the
server, not to a viewer: closing the panel leaves the deploy running, and
opening it again replays everything from the start and follows on. One
deploy runs per project at a time; a second request attaches to it.

A watch is the same with ``launchpad.py watch``, shared by its viewers and
stopped once the last one has left. Preflight, apps, link and unlink are
short runs answered as JSON.

The script gets a whole, minimal environment (nothing else of the server's):
the Space's XO key and the GitHub connector's token, which never leave the
server, and the paths and proxies it needs. Values for the app's environment
reach it on stdin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from services.cowork_agent import project_layout
from services.errors import ServiceError
from services.git_clone import scrub
from services.launchpad import launchpad
from services.timestamps import now_iso
from utils.commands import run, spawn_stream

log = logging.getLogger(__name__)

SCRIPT = Path(launchpad.__file__).resolve()
REPO_ROOT = SCRIPT.parents[2]
HEARTBEAT_SECONDS = 15.0
#: How long a browser's EventSource waits before it reconnects (the SSE ``retry`` field).
RECONNECT_MS = 3000
MAX_EVENTS = 5000
WATCH_GRACE_SECONDS = 5.0
STOP_GRACE_SECONDS = 10.0
ONE_SHOT_SECONDS = 120.0
TERMINAL = frozenset({"done", "error", "needs", "replaced"})

#: What the script may see of the server's environment.
PASSED_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "XDG_CONFIG_HOME", "QUIRQ_STATE_ROOT", "QUIRQ_COMMAND_LOG", "QUIRQ_COMMAND_LOG_PATH",
    "QUIRQ_COMMAND_LOG_ARCHIVE", "XO_LAUNCHPAD_URL", "XO_LAUNCHPAD_CONFIG",
    # Where the script finds GitHub and xo-launchpad, and how fast it polls:
    # the defaults are github.com and this environment's own xo-launchpad.
    "LAUNCHPAD_GITHUB_API", "LAUNCHPAD_GITHUB_GIT", "XO_LAUNCHPAD_BIN", "LAUNCHPAD_POLL_SECONDS",
)

#: HTTP status for a short run's error code.
STATUS = {
    "not_linked": 404, "app_not_found": 404, "already_linked": 409, "not_a_github_app": 400,
    "config_invalid": 400, "needs_agent": 409, "launchpad_key_missing": 503, "launchpad_cli_missing": 503,
    "launchpad_key_invalid": 502, "launchpad_unreachable": 502, "launchpad_forbidden": 403,
    "github_unreachable": 502, "internal": 500,
}


class DeployRefused(ServiceError):
    """A deploy request that can't go ahead. ``fix`` and ``data`` travel
    to the client with the code and message."""

    def __init__(self, code: str, message: str, status: int = 400, *, fix: str | None = None,
                 data: dict[str, Any] | None = None, action: str | None = None) -> None:
        super().__init__(code, message, status=status)
        self.fix, self.data, self.action = fix, data, action


@dataclass
class Run:
    id: str
    project: str
    kind: str
    started_at: str
    events: list[dict[str, Any]] = field(default_factory=list)
    listeners: set[asyncio.Queue] = field(default_factory=set)
    process: Any = None
    task: asyncio.Task | None = None
    finished_at: str | None = None
    outcome: dict[str, Any] | None = None
    stopping: bool = False
    step: str | None = None
    seq: int = 0

    def summary(self) -> dict[str, Any]:
        last = next((e for e in reversed(self.events) if e.get("type") != "ping"), None)
        out = {"run_id": self.id, "kind": self.kind, "started_at": self.started_at, "step": self.step, "last": last}
        if self.outcome is not None:
            out.update(finished_at=self.finished_at or self.outcome.get("ts"), outcome=self.outcome)
        return out


_live: dict[tuple[str, str], Run] = {}
_last: dict[str, Run] = {}
#: Each project's last finished watch, so a viewer resuming after its outcome
#: is told there is nothing more (204) instead of starting a new watch.
_last_watch: dict[str, Run] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock(project: str) -> asyncio.Lock:
    return _locks.setdefault(project, asyncio.Lock())


def _running(project: str, kind: str) -> Run | None:
    """The project's run of ``kind`` that has not said its outcome yet."""
    run_ = _live.get((project, kind))
    return run_ if run_ is not None and run_.outcome is None else None


async def _settle(project: str, kind: str) -> None:
    """Let a run that has said its outcome finish exiting before another starts."""
    run_ = _live.get((project, kind))
    if run_ is not None and run_.task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(run_.task), timeout=STOP_GRACE_SECONDS)
        except asyncio.TimeoutError:
            await run_.process.terminate(2.0)


# ── the project ──────────────────────────────────────────────────────────────

def project_path(name: str) -> Path:
    """The project's folder, or 404. Only a folder directly in the projects
    root: never a path, a hidden folder or a link."""
    if (not isinstance(name, str) or not name or len(name) > 160 or name != name.strip()
            or name.startswith((".", "-")) or any(c in name for c in "/\\\x00")
            or any(ord(c) < 32 for c in name)):
        raise DeployRefused("project_not_found", "Project not found.", 404)
    path = project_layout.project_dir(name)
    if path.is_symlink() or not path.is_dir():
        raise DeployRefused("project_not_found", "Project not found.", 404)
    return path


# ── the script's environment ─────────────────────────────────────────────────

def _xo_key() -> str | None:
    from services.swarm_api import auth_token
    return auth_token()


#: How long a ``gh auth token`` answer is reused (a check is cheap; gh is a process).
GH_CACHE_SECONDS = 30.0
_gh_cache: tuple[float, str | None] | None = None


def _connector_token() -> str | None:
    from services.cowork_agent.connectors.github.common import get_github_token
    try:
        return get_github_token(read_only=True)
    except (OSError, ValueError):
        return None


async def _gh_cli_token() -> str | None:
    """The token of this machine's own ``gh`` login, if there is one."""
    global _gh_cache
    now = time.monotonic()
    if _gh_cache is not None and now - _gh_cache[0] < GH_CACHE_SECONDS:
        return _gh_cache[1]
    from services.cowork_agent.connectors.github.cli_auth import gh_login_token
    token = await gh_login_token()
    _gh_cache = (now, token)
    return token


async def github_login() -> tuple[str | None, str | None]:
    """The GitHub token a deploy uses, and where it came from: the token
    saved in Connections first, else this machine's ``gh`` login, else
    ``GH_TOKEN`` / ``GITHUB_TOKEN`` in the Space's environment. Whichever it
    is, its scopes are checked before anything happens (preflight, deploy)."""
    token = await asyncio.to_thread(_connector_token)
    if token:
        return token, "connections"
    token = await _gh_cli_token()
    if token:
        return token, "gh"
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value, "env"
    return None, None


async def script_env() -> dict[str, str]:
    env = {k: os.environ[k] for k in PASSED_ENV if os.environ.get(k)}
    env.update(PYTHONUNBUFFERED="1", GIT_TERMINAL_PROMPT="0")
    key = _xo_key()
    if key:
        env["XO_API_KEY"] = key
    token, source = await github_login()
    if token:
        env["GITHUB_TOKEN"] = token
        env["LAUNCHPAD_GITHUB_SOURCE"] = source
    return env


def cli_installed() -> bool:
    return launchpad.Launchpad("").available()


# ── check ────────────────────────────────────────────────────────────────────

async def check(name: str) -> dict[str, Any]:
    """The script's check (files only) plus what this server knows locally:
    which keys it holds, and the deploy running or last finished."""
    path = project_path(name)
    result = await asyncio.to_thread(launchpad.check, path)
    token, source = await github_login()
    result["prerequisites"] = {"github_connected": bool(token), "github_source": source,
                               "launchpad_key": bool(_xo_key()), "launchpad_cli": cli_installed()}
    live = _running(path.name, "deploy") or _running(path.name, "watch")
    result["running"] = live.summary() if live else None
    ending = _live.get((path.name, "deploy"))
    last = ending if ending is not None and ending.outcome is not None else _last.get(path.name)
    result["last"] = last.summary() if last else None
    return result


# ── long runs: deploy and watch ──────────────────────────────────────────────

def deploy_argv(options: dict[str, Any]) -> list[str]:
    """The script's options for a deploy request (already validated)."""
    argv: list[str] = []
    for key, flag in (("name", "--name"), ("repo_name", "--repo-name")):
        if options.get(key):
            argv.append(f"{flag}={options[key]}")
    if options.get("port"):
        argv.append(f"--port={int(options['port'])}")
    if options.get("health_path") is not None:
        argv.append(f"--health-path={options['health_path']}")
    if options.get("size"):
        argv.append(f"--size={options['size']}")
    if options.get("storage_path"):
        argv.append(f"--storage-path={options['storage_path']}")
    if options.get("storage_size"):
        argv.append(f"--storage-size={int(options['storage_size'])}")
    for key, flag in (("create_repo", "--create-repo"), ("recreate", "--recreate"), ("rebuild", "--rebuild")):
        if options.get(key):
            argv.append(flag)
    for name in options.get("sensitive") or ():
        argv.append(f"--sensitive={name}")
    return argv


async def _spawn(path: Path, kind: str, argv: list[str], stdin: bytes | None) -> Run:
    process = await spawn_stream([sys.executable, str(SCRIPT), kind, "--project", str(path), *argv],
                                 cwd=REPO_ROOT, env=await script_env(), label=f"launchpad {kind} {path.name}")
    try:
        if stdin:
            await process.write(stdin)
        await process.close_stdin()
    except (BrokenPipeError, ConnectionResetError):
        pass                                    # the script exited early; its events say why
    run_ = Run(id=uuid.uuid4().hex[:12], project=path.name, kind=kind, started_at=now_iso(), process=process)
    _live[(path.name, kind)] = run_
    run_.task = asyncio.create_task(_pump(run_), name=f"launchpad-{kind}-{path.name}")
    log.info("launchpad: %s %s started (run %s)", kind, path.name, run_.id)
    return run_


def _publish(run_: Run, event: dict[str, Any]) -> None:
    """Keep ``event`` and hand it to every viewer. Each event of a run gets
    the next ``seq`` and the ``run_id``: its SSE id is ``<run_id>:<seq>``, so
    a viewer that reconnects asks for what came after it."""
    run_.seq += 1
    event = {**event, "run_id": run_.id, "seq": run_.seq}
    run_.events.append(event)
    if len(run_.events) > MAX_EVENTS:
        del run_.events[1:len(run_.events) - MAX_EVENTS + 1]
    if event.get("type") == "step":
        run_.step = event.get("step")
    if event.get("type") in TERMINAL and run_.outcome is None:
        run_.outcome = event
    for queue in list(run_.listeners):
        queue.put_nowait(event)


async def _pump(run_: Run) -> None:
    try:
        async for raw in run_.process.lines():
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("type"), str):
                _publish(run_, event)
        code = await run_.process.wait()
        if run_.outcome is None:
            info = launchpad.flow()["failures"]["cancelled" if run_.stopping else "internal"]
            message = info["message"] if run_.stopping else (
                f"{info['message']} (the script exited with {code} without saying why)")
            code = "cancelled" if run_.stopping else "internal"
            _publish(run_, {"type": "error", "ts": now_iso(), "step": run_.step, "code": code, "message": message,
                            "fix": info["fix"], "action": launchpad.action(code)})
            if not run_.stopping:
                log.warning("launchpad: %s %s exited %s without an outcome; its stderr ends:\n%s", run_.kind,
                            run_.project, code, scrub(run_.process.stderr_tail()))
    except Exception:  # noqa: BLE001 - the viewers still get an outcome
        log.exception("launchpad: following %s %s failed", run_.kind, run_.project)
        if run_.outcome is None:
            info = launchpad.flow()["failures"]["internal"]
            _publish(run_, {"type": "error", "ts": now_iso(), "step": run_.step, "code": "internal",
                            "message": info["message"], "fix": info["fix"], "action": launchpad.action("internal")})
    finally:
        run_.finished_at = now_iso()
        if _live.get((run_.project, run_.kind)) is run_:
            del _live[(run_.project, run_.kind)]
        if run_.kind == "deploy":
            _last[run_.project] = run_
        else:
            _last_watch[run_.project] = run_
        for queue in list(run_.listeners):
            queue.put_nowait(None)
        outcome = run_.outcome or {}
        log.info("launchpad: %s %s finished: %s %s", run_.kind, run_.project, outcome.get("type"),
                 outcome.get("code") or "")


async def start_deploy(name: str, options: dict[str, Any], env_values: dict[str, str]) -> tuple[Run, bool]:
    """The project's deploy: started now, or the one already running
    (``attached``)."""
    path = project_path(name)
    async with _lock(path.name):
        live = _running(path.name, "deploy")
        if live is not None:
            return live, True
        await _settle(path.name, "deploy")
        argv = deploy_argv(options)
        stdin = None
        if env_values:
            argv.append("--secrets-stdin")
            stdin = json.dumps({"env": env_values}).encode()
        deploy = await _spawn(path, "deploy", argv, stdin)
        watch = _running(path.name, "watch")
        if watch is not None:
            # Its viewers follow the deploy now (it shows the build and release itself).
            _publish(watch, {"type": "replaced", "ts": now_iso(), "by": {"run_id": deploy.id, "kind": "deploy"}})
            watch.stopping = True
            asyncio.get_running_loop().create_task(watch.process.terminate(2.0))
        return deploy, False


def parse_resume(last_event_id: str | None, run: str | None, after: int | None) -> tuple[str, int] | None:
    """Where a viewer left off: ``Last-Event-ID`` (``<run_id>:<seq>``), else
    ``?run=&after=``."""
    if last_event_id and ":" in last_event_id:
        run_id, _, seq = last_event_id.rpartition(":")
        if run_id and seq.isdigit():
            return run_id, int(seq)
    if run and after is not None and after >= 0:
        return run, after
    return None


async def start_watch(name: str, resume: tuple[str, int] | None = None) -> tuple[Run, bool, int] | None:
    """What a viewer watches: the run it is resuming (from after its last
    event), the running deploy, else the project's shared watch. ``None``
    when the run it resumes has already told it its outcome: nothing more
    to say (a browser's EventSource stops reconnecting on 204)."""
    path = project_path(name)
    async with _lock(path.name):
        if resume is not None:
            run_id, after = resume
            known = [r for r in (_live.get((path.name, "deploy")), _live.get((path.name, "watch")),
                                 _last.get(path.name), _last_watch.get(path.name))
                     if r is not None and r.id == run_id]
            if known:
                run_ = known[0]
                outcome_seq = (run_.outcome or {}).get("seq")
                if outcome_seq is not None and after >= outcome_seq:
                    return None
                return run_, True, after
        for kind in ("deploy", "watch"):
            live = _running(path.name, kind)
            if live is not None:
                return live, True, 0
        await _settle(path.name, "watch")
        return await _spawn(path, "watch", [], None), False, 0


async def stop_deploy(name: str) -> dict[str, Any]:
    """Stop the project's deploy: SIGTERM, so the script says so and ends;
    the whole group is killed if it hasn't after a grace period."""
    path = project_path(name)
    run_ = _running(path.name, "deploy")
    if run_ is None:
        return {"stopped": False}
    run_.stopping = True
    await run_.process.signal(15)
    try:
        await asyncio.wait_for(asyncio.shield(run_.task), timeout=STOP_GRACE_SECONDS)
    except asyncio.TimeoutError:
        await run_.process.terminate(2.0)
    return {"stopped": True, "run_id": run_.id}


def sse(event: dict[str, Any]) -> str:
    """One SSE frame; a run's own events carry their id for resuming."""
    head = f"id: {event['run_id']}:{event['seq']}\n" if "seq" in event and "run_id" in event else ""
    return f"{head}data: {json.dumps(event)}\n\n"


async def stream(run_: Run, attached: bool, after: int = 0) -> AsyncIterator[str]:
    """The run's events as SSE: those after ``after`` so far, then live,
    until its outcome."""
    queue: asyncio.Queue = asyncio.Queue()
    backlog = [e for e in run_.events if e["seq"] > after]   # snapshot and subscribe with no await between
    finished = run_.finished_at is not None
    run_.listeners.add(queue)
    try:
        yield f"retry: {RECONNECT_MS}\n" + sse({"type": "run", "ts": now_iso(), "run_id": run_.id,
                                                "kind": run_.kind, "attached": attached,
                                                "started_at": run_.started_at, "after": after})
        for event in backlog:
            yield sse(event)
            if event.get("type") in TERMINAL:
                return
        if finished:
            return
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n" + sse({"type": "ping", "ts": now_iso()})
                continue
            if item is None:
                return
            yield sse(item)
            if item.get("type") in TERMINAL:
                return
    finally:
        run_.listeners.discard(queue)
        if run_.kind == "watch" and not run_.listeners and run_.finished_at is None:
            asyncio.get_running_loop().create_task(_stop_unwatched(run_))


async def _stop_unwatched(run_: Run) -> None:
    await asyncio.sleep(WATCH_GRACE_SECONDS)
    if not run_.listeners and run_.finished_at is None:
        run_.stopping = True
        await run_.process.terminate(2.0)


# ── short runs: preflight, apps, link, unlink ────────────────────────────────

async def _one_shot(path: Path | None, kind: str, argv: list[str]) -> dict[str, Any]:
    project = ["--project", str(path)] if path is not None else []
    result = await run([sys.executable, str(SCRIPT), kind, *project, *argv], cwd=REPO_ROOT,
                       env=await script_env(), timeout=ONE_SHOT_SECONDS, separate_stderr=True, log_output=False,
                       log_label=f"launchpad {kind}")
    events = []
    for line in result.output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type"):
            events.append(event)
    last = events[-1] if events else {}
    if last.get("type") == "result":
        out = {k: v for k, v in last.items() if k not in ("type", "ts")}
        warnings = [e["message"] for e in events if e.get("type") == "warning"]
        if warnings:
            out["warnings"] = warnings
        return out
    if last.get("type") in ("error", "needs"):
        code = last.get("code") or "internal"
        raise DeployRefused(code, last.get("message") or code, STATUS.get(code, 400), fix=last.get("fix"),
                            data=last.get("data"), action=last.get("action"))
    log.warning("launchpad: %s %s gave no result (exit %s, timed out %s); its stderr ends:\n%s", kind,
                path.name if path else "-", result.returncode, result.timed_out, scrub("\n".join((result.stderr or "").splitlines()[-20:])))
    info = launchpad.flow()["failures"]["internal"]
    raise DeployRefused("internal", info["message"], 500, fix=info["fix"], action=launchpad.action("internal"))


#: How long Launchpad's size catalog is reused: it changes with Launchpad releases, not with deploys.
SIZES_CACHE_SECONDS = 600.0
_sizes_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def sizes(template: str = "github") -> dict[str, Any]:
    """The machine sizes, with ``template``'s default and allowed ones
    (``xo-launchpad sizes``), reused for ``SIZES_CACHE_SECONDS``. Launchpad
    out of reach: flow.json's tiers, with no default (``source``)."""
    cached = _sizes_cache.get(template)
    if cached is not None and time.monotonic() - cached[0] < SIZES_CACHE_SECONDS:
        return cached[1]
    try:
        found = (await _one_shot(None, "sizes", [f"--template={template}"]))["sizes"]
    except DeployRefused as exc:
        table = launchpad.flow()["sizes"]
        return {"tiers": table["tiers"], "default": None, "allowed": [s["name"] for s in table["tiers"]],
                "template": template, "fixed_after_create": True, "source": "flow.json", "error": exc.code}
    _sizes_cache[template] = (time.monotonic(), found)
    return found


_preflights: dict[tuple[str, str | None], asyncio.Future] = {}


async def preflight(name: str, app_name: str | None = None) -> dict[str, Any]:
    """The script's preflight. Asks made while one is running share it: a
    panel that opens twice asks GitHub and Launchpad once."""
    path = project_path(name)
    key = (path.name, app_name)
    shared = _preflights.get(key)
    if shared is None:
        shared = asyncio.ensure_future(_one_shot(path, "preflight", [f"--name={app_name}"] if app_name else []))
        _preflights[key] = shared
        def forget(done: asyncio.Future) -> None:
            _preflights.pop(key, None)
            if not done.cancelled():
                done.exception()                # read, so a failure no waiter saw is not logged as lost
        shared.add_done_callback(forget)
    return (await asyncio.shield(shared))["preflight"]


async def apps(name: str) -> dict[str, Any]:
    return await _one_shot(project_path(name), "apps", [])


async def link(name: str, app: str) -> dict[str, Any]:
    path = project_path(name)
    async with _lock(path.name):
        _no_deploy(path.name)
        return await _one_shot(path, "link", [f"--app={app}"])


async def unlink(name: str, forget: bool) -> dict[str, Any]:
    path = project_path(name)
    async with _lock(path.name):
        _no_deploy(path.name)
        return await _one_shot(path, "unlink", ["--forget"] if forget else [])


def _no_deploy(project: str) -> None:
    if _running(project, "deploy") is not None:
        raise DeployRefused("deploy_running", "A deploy is running for this project; wait for it to finish.", 409)


async def shutdown() -> None:
    """Stop every run (server shutdown)."""
    for run_ in list(_live.values()):
        run_.stopping = True
        try:
            await run_.process.terminate(2.0)
        except Exception:  # noqa: BLE001 - best effort at shutdown
            log.debug("launchpad: stopping %s %s failed", run_.kind, run_.project, exc_info=True)
