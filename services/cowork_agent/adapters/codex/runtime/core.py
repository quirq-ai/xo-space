"""The codex runtime: storm's contract (``adapters/contract.py``) on codex's
app-server (storm ch 03).

Loaded as capability ``runtime``: ``load_capability("runtime",
agent="codex").runtime()`` returns the one ``CodexRuntime``. Core opens
sessions with ``await runtime.open(spec, host, session_id=...)``; each
session runs its own ``codex app-server`` (owner, 2026-10-22). Everything
CLI-specific (the protocol, the item shapes, the env, codex's files) stays in
this package, which imports no FastAPI, no storm and no other agent.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from services.cowork_agent.adapters.cli_status import resolve_binary
from services.cowork_agent.adapters.codex.runtime import support, transcript
from services.cowork_agent.adapters.codex.runtime.account import (
    UsageGuard,
    account_fingerprint,
    logged_in,
    login_fingerprint,
)
from services.cowork_agent.adapters.codex.runtime.env import codex_home, credential_vars, session_env
from services.cowork_agent.adapters.codex.runtime.rpc import Rpc, classify, initialize_params
from services.cowork_agent.adapters.codex.runtime.session import CodexSession
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
from utils.commands import spawn_stream

CLI_PATH_ENV = "CODEX_CLI_PATH"
DEFAULT_BINARY = "codex"
PROBE_TIMEOUT_S = 30.0
KILL_GRACE_S = 2.0
#: How long a failed model probe is answered from memory instead of starting
#: codex again (``/api/models`` would otherwise start a process per call).
MODELS_FAILURE_TTL_S = 30.0
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_VERSION = re.compile(r"(\d+\.\d+\.\d+\S*)")


def find_binary(home: Path) -> str:
    """``CODEX_CLI_PATH``, then PATH, then where codex's own installer puts it
    (a server's PATH often lacks ``~/.local/bin``); else the bare name, which
    fails to start as ``cli_missing``."""
    found = resolve_binary(CLI_PATH_ENV, DEFAULT_BINARY)
    if found != DEFAULT_BINARY:
        return found
    standalone = home / "packages" / "standalone" / "current"
    for candidate in (Path.home() / ".local" / "bin" / "codex", standalone / "bin" / "codex", standalone / "codex"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return DEFAULT_BINARY


class _Executor:
    """``contract.Launcher`` on the one executor, for the runtime's own
    model-free probes (sessions start through the Host's launcher)."""

    async def spawn(self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], label: str):
        return await spawn_stream(argv, cwd=cwd, env=env, label=label)


class CodexRuntime:
    name = support.AGENT
    contract_version = CONTRACT_VERSION

    def __init__(self, *, launcher: Launcher | None = None, home: Path | None = None) -> None:
        self._launcher = launcher or _Executor()
        self._home = home
        self._guards: dict[str | None, UsageGuard] = {}      # one per account
        self._versions: dict[tuple[str, int], str] = {}
        self._models: dict[tuple[str, int, str], list[ModelInfo]] = {}
        self._models_failure: tuple[tuple[str, int, str], float, RuntimeFailure] | None = None
        self._models_probe: tuple[tuple[str, int, str], concurrent.futures.Future] | None = None
        self._models_lock = threading.Lock()
        self._cli_version: str | None = None

    @property
    def codex_home(self) -> Path:
        return self._home or codex_home()

    def binary(self) -> str:
        return find_binary(self.codex_home)

    # ── what it offers ───────────────────────────────────────────────────

    def check(self, spec: SessionSpec) -> SupportReport:
        return support.check(spec)

    def features(self) -> FeatureList:
        return support.features(self._cli_version, auth_env=credential_vars())

    async def describe(self) -> RuntimeInfo:
        binary = self.binary()
        key = (binary, await asyncio.to_thread(_mtime_ns, binary))
        if key not in self._versions:
            self._versions = {key: await self._version(binary)}
        self._cli_version = self._versions[key]
        home = self.codex_home
        signed_in = await asyncio.to_thread(logged_in, home)
        account = await self._account() if signed_in else None
        return RuntimeInfo(cli_path=binary, cli_version=self._cli_version, logged_in=signed_in, account=account,
                           limits={}, probes={"sandbox": "not used: every session runs danger-full-access (§0.67)"})

    async def _version(self, binary: str) -> str:
        env = await asyncio.to_thread(session_env, spec_env={}, label="codex probe")
        handle = await _spawn(self._launcher, [binary, "--version"], cwd=str(Path.home()), env=env,
                              label="codex probe")
        try:
            out = await asyncio.wait_for(_collect(handle), PROBE_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise RuntimeFailure("timeout", f"{binary} --version didn't finish", retryable=True) from exc
        finally:
            await handle.terminate(KILL_GRACE_S)
        code, lines = out
        found = _VERSION.search(b" ".join(lines).decode(errors="replace")) if code == 0 else None
        if found is None:
            raise RuntimeFailure("cli_missing", f"{binary} --version failed (exit {code})")
        return found.group(1)

    async def models(self) -> list[ModelInfo]:
        """codex's own model list (``model/list``), from a model-free
        app-server; kept per installed binary and login. One probe at a
        time: callers that arrive while it runs wait for it and share its
        answer; a failed probe is kept for ``MODELS_FAILURE_TTL_S``. The same
        probe reads the account's rate limits, so ``usage()`` has them."""
        binary = self.binary()
        key = (binary, await asyncio.to_thread(_mtime_ns, binary),
               await asyncio.to_thread(login_fingerprint, self.codex_home))
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
            found, limits = await self._probe_models(binary)
        except BaseException as exc:
            failure_out = exc if isinstance(exc, RuntimeFailure) else \
                RuntimeFailure("timeout", "reading codex's model list was cancelled; ask again", retryable=True)
            with self._models_lock:
                if isinstance(exc, RuntimeFailure):
                    self._models_failure = (key, time.monotonic(), exc)
                if self._models_probe is not None and self._models_probe[1] is mine:
                    self._models_probe = None
            mine.set_exception(failure_out)
            raise
        if limits is not None:
            self._guard_for(await self._account()).update(limits)
        with self._models_lock:
            self._models, self._models_failure = {key: found}, None
            if self._models_probe is not None and self._models_probe[1] is mine:
                self._models_probe = None
        mine.set_result(found)
        return list(found)

    async def _probe_models(self, binary: str) -> tuple[list[ModelInfo], Any]:
        """A short-lived app-server in an empty folder: ``initialize``,
        ``model/list`` (every page), ``account/rateLimits/read``. No thread
        starts, so no model is called and nothing is written."""
        env = await asyncio.to_thread(session_env, spec_env={}, label="codex models probe")
        try:
            folder = tempfile.TemporaryDirectory(prefix="xo-codex-probe-")
        except OSError as exc:
            raise RuntimeFailure("cli_missing", f"no temp folder for the codex probe: {exc.strerror or exc}") from exc
        with folder as cwd:
            handle = await _spawn(self._launcher, [binary, "app-server", "--listen", "stdio://"], cwd=cwd, env=env,
                                  label="codex models probe")

            async def write(data: bytes) -> None:
                await handle.write(data)

            rpc = Rpc(write)

            async def pump() -> None:
                async for raw in handle.lines():
                    line = classify(raw)
                    if line.kind == "response":
                        rpc.resolve(line.msg)
                    elif line.kind == "request":
                        await rpc.respond_error(line.msg.get("id"), -32601, "a probe serves nothing")
                rpc.fail_all(RuntimeFailure("process_died", "codex exited before answering the probe"))

            reader = asyncio.create_task(pump(), name="codex-models-probe")
            try:
                await rpc.request("initialize", initialize_params(), timeout=PROBE_TIMEOUT_S)
                await rpc.notify("initialized")
                rows: list[Any] = []
                cursor = None
                for _ in range(20):
                    page = await rpc.request("model/list", {"cursor": cursor} if cursor else {},
                                             timeout=PROBE_TIMEOUT_S)
                    rows += page.get("data") if isinstance(page.get("data"), list) else []
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                limits = None
                try:
                    read = await rpc.request("account/rateLimits/read", {}, timeout=PROBE_TIMEOUT_S)
                    limits = read.get("rateLimits")
                except RuntimeFailure:
                    pass                    # not logged in, or an API key without limits: no usage to show
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeFailure("process_died", "codex exited before answering the probe") from exc
            finally:
                reader.cancel()
                await asyncio.wait({reader}, timeout=KILL_GRACE_S)
                await handle.terminate(KILL_GRACE_S)
        return models_from(rows), limits

    async def usage(self) -> UsageSnapshot:
        """The current account's last rate-limit state."""
        return self._guard_for(await self._account()).snapshot()

    async def _account(self) -> str | None:
        return await asyncio.to_thread(account_fingerprint, self.codex_home)

    def _guard_for(self, account: str | None) -> UsageGuard:
        return self._guards.setdefault(account, UsageGuard())

    # ── sessions ─────────────────────────────────────────────────────────

    async def open(self, spec: SessionSpec, host: Host, *, session_id: str) -> CodexSession:
        if not _SESSION_ID.fullmatch(session_id):
            raise RuntimeFailure("spec_refused", f"not a session id: {session_id!r} (letters, digits, _ and -)")
        report = self.check(spec)
        session = CodexSession(session_id=session_id, spec=spec, report=report, host=host, binary=self.binary(),
                               codex_home=self.codex_home, guard=self._guard_for(await self._account()))
        await session.start()
        if session.cli_version:
            self._cli_version = session.cli_version
        return session

    # ── codex's own files ────────────────────────────────────────────────

    async def list_sessions(self, *, cwd: str | None = None, limit: int = 50) -> list[SessionInfo]:
        return await asyncio.to_thread(transcript.list_sessions, self.codex_home, cwd=cwd, limit=limit)

    async def read_session(self, native_id: str, *, after: str | None = None, limit: int = 50) -> TranscriptPage:
        return await asyncio.to_thread(transcript.read_session, self.codex_home, native_id, after=after, limit=limit)

    async def list_runs(self, native_id: str) -> list[RunInfo]:
        return await asyncio.to_thread(transcript.list_runs, self.codex_home, native_id)

    async def read_run(self, native_id: str, run_id: str, *, after: str | None = None,
                       limit: int = 50) -> TranscriptPage:
        return await asyncio.to_thread(transcript.read_run, self.codex_home, native_id, run_id, after=after,
                                       limit=limit)


def models_from(rows: Sequence[Any]) -> list[ModelInfo]:
    """``model/list`` rows: hidden ones left out; the efforts the contract
    names (codex's ``ultra`` has no contract value yet)."""
    out = []
    for row in rows:
        if not isinstance(row, dict) or row.get("hidden"):
            continue
        model_id = row.get("id") or row.get("model")
        if not isinstance(model_id, str) or not model_id:
            continue
        offered = row.get("supportedReasoningEfforts") if isinstance(row.get("supportedReasoningEfforts"), list) \
            else []
        names = [e.get("reasoningEffort") if isinstance(e, dict) else e for e in offered]
        efforts = tuple(e for e in support.EFFORTS if e in names)
        default = row.get("defaultReasoningEffort")
        description = row.get("description")
        out.append(ModelInfo(id=model_id, name=row.get("displayName") or model_id, efforts=efforts,
                             default_effort=default if default in support.EFFORTS else None,
                             is_default=bool(row.get("isDefault")), takes_effort=bool(names),
                             description=description if isinstance(description, str) and description else None))
    return out


async def _spawn(launcher: Launcher, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], label: str) -> Any:
    try:
        return await launcher.spawn(argv, cwd=cwd, env=env, label=label)
    except OSError as exc:
        raise RuntimeFailure("cli_missing", f"the codex CLI can't be started ({argv[0]}: {exc.strerror or exc}); "
                                            "install it, or point CODEX_CLI_PATH at an executable codex") from exc


async def _collect(handle: Any) -> tuple[int, list[bytes]]:
    lines = [line async for line in handle.lines()]
    return await handle.wait(), lines


async def _wait_shared(shared: concurrent.futures.Future) -> list[ModelInfo]:
    result = asyncio.wrap_future(shared)
    result.add_done_callback(lambda f: f.cancelled() or f.exception())
    try:
        return await asyncio.wait_for(asyncio.shield(result), PROBE_TIMEOUT_S * 3 + KILL_GRACE_S)
    except asyncio.TimeoutError as exc:
        raise RuntimeFailure("timeout", "codex's model list is still being read; ask again shortly",
                             retryable=True) from exc


def _mtime_ns(binary: str) -> int:
    path = shutil.which(binary) or binary
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return 0


_RUNTIME: CodexRuntime | None = None


def runtime() -> CodexRuntime:
    """The codex runtime: one per server, so its rate-limit guards (one per
    account) and model list are shared by every session."""
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = CodexRuntime()
    return _RUNTIME
