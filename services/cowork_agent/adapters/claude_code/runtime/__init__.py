"""The claude_code runtime: storm's contract (``adapters/contract.py``) on
Claude Code's stream-json control protocol (storm ch 02).

Loaded as capability ``runtime``: ``load_capability("runtime",
agent="claude_code").runtime()`` returns the one ``ClaudeRuntime``. Core opens
sessions with ``await runtime.open(spec, host, session_id=...)``; everything
CLI-specific (flags, wire format, tool names, env, files) stays in this
package, which imports no FastAPI, no storm and no other agent.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from services.cowork_agent.adapters.claude_code.runtime import probe, support, transcript
from services.cowork_agent.adapters.claude_code.runtime.account import UsageGuard, login_fingerprint
from services.cowork_agent.adapters.claude_code.runtime.command import credential_ask_rules
from services.cowork_agent.adapters.claude_code.runtime.env import session_env
from services.cowork_agent.adapters.claude_code.runtime.session import ClaudeSession
from services.cowork_agent.adapters.cli_status import resolve_binary
from services.cowork_agent.adapters.contract import (
    CONTRACT_VERSION,
    FeatureList,
    Host,
    Launcher,
    ModelInfo,
    RunInfo,
    RuntimeFailure,
    RuntimeInfo,
    SessionInfo,
    SessionSpec,
    SupportReport,
    TranscriptPage,
    UsageSnapshot,
)
from services.storage.layout import run_dir
from services.storage.paths import quirq_state_dir
from utils.commands import spawn_stream

CLI_PATH_ENV = "CLAUDE_CLI_PATH"
DEFAULT_BINARY = "claude"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
#: How long a failed model probe is answered from memory instead of starting
#: claude again (a missing or hung CLI would otherwise cost a process, and up to
#: the probe's timeout, on every /api/models call).
MODELS_FAILURE_TTL_S = 30.0
#: Claude Code's own built-in limits on sub-agents (ch 01 §3.6).
BUILT_IN_LIMITS = {"max_subagents": 20, "max_depth": 3}
#: Where agy keeps its login; a credential path every claude session asks
#: about (ch 16 §11.5). Named here, not imported: an adapter never imports
#: another agent's.
AGY_TOKEN_FILE = Path("~/.gemini/antigravity-cli/antigravity-oauth-token")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class _Executor:
    """``contract.Launcher`` on the one executor, for the runtime's own
    model-free probes (sessions start through the Host's launcher)."""

    async def spawn(self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], label: str):
        return await spawn_stream(argv, cwd=cwd, env=env, label=label)


class ClaudeRuntime:
    name = support.AGENT
    contract_version = CONTRACT_VERSION

    def __init__(self, *, launcher: Launcher | None = None, claude_home: Path | None = None) -> None:
        self._launcher = launcher or _Executor()
        self._claude_home = claude_home
        self._guard = UsageGuard()
        self._models: dict[tuple[str, int, str], list[ModelInfo]] = {}
        self._models_failure: tuple[tuple[str, int, str], float, RuntimeFailure] | None = None
        self._models_probe: tuple[tuple[str, int, str], concurrent.futures.Future] | None = None
        self._models_lock = threading.Lock()
        self._cli_version: str | None = None

    @property
    def claude_home(self) -> Path:
        return self._claude_home or Path.home() / ".claude"

    def binary(self) -> str:
        return resolve_binary(CLI_PATH_ENV, DEFAULT_BINARY)

    async def _probe_env(self) -> dict[str, str]:
        return await asyncio.to_thread(session_env, switches={}, spec_env={}, label="claude probe")

    # ── what it offers ───────────────────────────────────────────────────

    def check(self, spec: SessionSpec) -> SupportReport:
        return support.check(spec)

    def features(self) -> FeatureList:
        return support.features(self._cli_version)

    async def describe(self) -> RuntimeInfo:
        binary, env, home = self.binary(), await self._probe_env(), str(Path.home())
        code, out = await probe.run(self._launcher, [binary, "--version"], env=env, cwd=home)
        version = out[0].decode(errors="replace").split()[0] if code == 0 and out and out[0].strip() else None
        if version is None:
            raise RuntimeFailure("cli_missing", f"{binary} --version failed (exit {code})")
        self._cli_version = version
        code, out = await probe.run(self._launcher, [binary, "auth", "status", "--json"], env=env, cwd=home)
        try:
            status = json.loads(b"\n".join(out)) if out else {}
        except ValueError:
            status = {}
        logged_in = bool(isinstance(status, dict) and status.get("loggedIn"))
        account = None
        if logged_in:
            account = status.get("subscriptionType") or status.get("authMethod") or "logged in"
        return RuntimeInfo(cli_path=binary, cli_version=version, logged_in=logged_in, account=str(account)
                           if account else None, limits=dict(BUILT_IN_LIMITS),
                           probes={"sandbox": "not run by this runtime", "auth": status.get("authMethod")
                                   if isinstance(status, dict) else None})

    async def models(self) -> list[ModelInfo]:
        """claude's own model list, from a model-free ``initialize``; kept per
        installed binary and login. One probe at a time: callers that arrive
        while it runs wait for it (at most the probe's own timeout) and share
        its answer, success or failure. A failed probe is kept for
        ``MODELS_FAILURE_TTL_S``, so ``/api/models`` never starts a CLI per
        call; a changed binary or login asks again at once.

        ``/api/models`` calls this on several worker threads, each with its
        own event loop, so the shared state sits behind a thread lock (held
        only for bookkeeping, never across an await) and waiters wait on a
        ``concurrent.futures.Future`` the prober sets."""
        binary = self.binary()
        key = (binary, await asyncio.to_thread(_mtime_ns, binary),
               await asyncio.to_thread(login_fingerprint, self.claude_home))
        with self._models_lock:
            if key in self._models:
                return list(self._models[key])
            failure = self._models_failure
            if failure is not None and failure[0] == key and time.monotonic() - failure[1] < MODELS_FAILURE_TTL_S:
                raise RuntimeFailure(failure[2].code, f"{failure[2].message} (failed in the last "
                                                      f"{MODELS_FAILURE_TTL_S:g} s; not asked again yet)",
                                     retryable=True)
            running = self._models_probe
            if running is not None and running[0] == key:
                shared = running[1]
            else:
                shared = None
                mine: concurrent.futures.Future = concurrent.futures.Future()
                self._models_probe = (key, mine)
        if shared is not None:
            return list(await _wait_shared(shared))
        try:
            reply = await probe.initialize(self._launcher, binary, env=await self._probe_env())
        except BaseException as exc:
            failure_out = exc if isinstance(exc, RuntimeFailure) else \
                RuntimeFailure("timeout", "reading claude's model list was cancelled; ask again", retryable=True)
            with self._models_lock:
                if isinstance(exc, RuntimeFailure):
                    self._models_failure = (key, time.monotonic(), exc)
                if self._models_probe is not None and self._models_probe[1] is mine:
                    self._models_probe = None
            mine.set_exception(failure_out)
            raise
        found = _models(reply.get("models"))
        with self._models_lock:
            if isinstance(reply.get("claude_code_version"), str):
                self._cli_version = reply["claude_code_version"]
            self._models, self._models_failure = {key: found}, None
            if self._models_probe is not None and self._models_probe[1] is mine:
                self._models_probe = None
        mine.set_result(found)
        return list(found)

    async def usage(self) -> UsageSnapshot:
        return self._guard.snapshot()

    # ── sessions ─────────────────────────────────────────────────────────

    async def open(self, spec: SessionSpec, host: Host, *, session_id: str) -> ClaudeSession:
        if not _SESSION_ID.fullmatch(session_id):
            raise RuntimeFailure("spec_refused", f"not a session id: {session_id!r} (letters, digits, _ and -)")
        report = self.check(spec)
        native_id = spec.resume if spec.resume and not spec.fork else str(uuid.uuid4())
        home = Path.home()
        rules = credential_ask_rules(home, quirq_state_dir(), AGY_TOKEN_FILE.expanduser())
        session = ClaudeSession(session_id=session_id, native_id=native_id, spec=spec, report=report, host=host,
                                binary=self.binary(), folder=run_dir() / session_id, claude_home=self.claude_home,
                                guard=self._guard, credential_rules=rules)
        await session.start(resume=spec.resume)
        if session.cli_version:
            self._cli_version = session.cli_version
        return session

    # ── claude's own files ───────────────────────────────────────────────

    async def list_sessions(self, *, cwd: str | None = None, limit: int = 50) -> list[SessionInfo]:
        return await asyncio.to_thread(transcript.list_sessions, self.claude_home, cwd=cwd, limit=limit)

    async def read_session(self, native_id: str, *, after: str | None = None, limit: int = 50) -> TranscriptPage:
        return await asyncio.to_thread(transcript.read_session, self.claude_home, native_id, after=after,
                                       limit=limit)

    async def list_runs(self, native_id: str) -> list[RunInfo]:
        return await asyncio.to_thread(transcript.list_runs, self.claude_home, native_id)

    async def read_run(self, native_id: str, run_id: str, *, after: str | None = None,
                       limit: int = 50) -> TranscriptPage:
        return await asyncio.to_thread(transcript.read_run, self.claude_home, native_id, run_id, after=after,
                                       limit=limit)


async def _wait_shared(shared: concurrent.futures.Future) -> list[ModelInfo]:
    """Wait for another caller's probe, at most as long as the probe itself
    may take. The shield keeps a waiter that gives up from cancelling the
    probe's result for everyone else."""
    result = asyncio.wrap_future(shared)
    result.add_done_callback(lambda f: f.cancelled() or f.exception())   # retrieved even if we gave up
    try:
        return await asyncio.wait_for(asyncio.shield(result), probe.INITIALIZE_PROBE_TIMEOUT_S + probe.KILL_GRACE_S)
    except asyncio.TimeoutError as exc:
        raise RuntimeFailure("timeout", "claude's model list is still being read; ask again shortly",
                             retryable=True) from exc


def _mtime_ns(binary: str) -> int:
    path = shutil.which(binary) or binary
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return 0


def _models(raw: object) -> list[ModelInfo]:
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
            continue
        efforts = entry.get("supportedEffortLevels") if isinstance(entry.get("supportedEffortLevels"), list) else []
        out.append(ModelInfo(id=entry["value"], name=entry.get("displayName") or entry["value"],
                             efforts=tuple(e for e in efforts if e in EFFORTS),
                             is_default=entry["value"] == "default"))
    return out


_RUNTIME: ClaudeRuntime | None = None


def runtime() -> ClaudeRuntime:
    """The claude runtime: one per server, so its rate-limit guard and model
    list are shared by every session."""
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = ClaudeRuntime()
    return _RUNTIME
