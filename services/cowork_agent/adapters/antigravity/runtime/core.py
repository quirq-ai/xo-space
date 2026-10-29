"""The agy runtime: storm's contract (``adapters/contract.py``) on agy's
headless stream-json (storm ch 04).

Loaded as capability ``runtime``: ``load_capability("runtime",
agent="antigravity").runtime()`` returns the one ``AgyRuntime``. Core opens
sessions with ``await runtime.open(spec, host, session_id=...)``; each runs
its own ``agy`` process (and another after an interrupt or an update, on the
same conversation). Everything agy-specific (the wire, the hook, the plugin,
agy's files) stays in this package, which imports no FastAPI, no storm and no
other agent.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from services.cowork_agent.adapters.antigravity.runtime import install, support, transcript
from services.cowork_agent.adapters.antigravity.runtime.account import (
    UsageGuard,
    account_fingerprint,
    logged_in,
    parse_usage,
)
from services.cowork_agent.adapters.antigravity.runtime.env import AUTH_VARS, probe_env
from services.cowork_agent.adapters.antigravity.runtime.hooks import socket_folder
from services.cowork_agent.adapters.antigravity.runtime.session import AgySession
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
from utils.commands import spawn_stream

CLI_PATH_ENV = "AGY_CLI_PATH"
DEFAULT_BINARY = "agy"
PROBE_TIMEOUT_S = 60.0
KILL_GRACE_S = 2.0
#: How long a failed model or usage probe is answered from memory.
FAILURE_TTL_S = 30.0
#: How long agy's usage report is trusted before it is read again.
USAGE_TTL_S = 300.0
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_VERSION = re.compile(r"(\d+\.\d+\.\d+\S*)")
_MODEL_ROW = re.compile(r"^([A-Za-z0-9][\w.:-]*)\t(.+?)\s*$")


def find_binary() -> str:
    """``AGY_CLI_PATH``, then PATH, then where agy's installer puts it (a
    server's PATH often lacks ``~/.local/bin``); else the bare name, which
    fails to start as ``cli_missing``."""
    found = resolve_binary(CLI_PATH_ENV, DEFAULT_BINARY)
    if found != DEFAULT_BINARY:
        return found
    candidate = Path(os.environ.get("HOME") or str(Path.home())) / ".local" / "bin" / "agy"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return DEFAULT_BINARY


class _Executor:
    """``contract.Launcher`` for the runtime's own model-free probes
    (sessions start through the Host's launcher)."""

    async def spawn(self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], label: str):
        return await spawn_stream(argv, cwd=cwd, env=env, label=label)


def models_from(text: str) -> list[ModelInfo]:
    """``agy models``: one ``id TAB name`` row per model (agy has no JSON
    form); its default is the first. Each model takes ``--effort``."""
    rows = [m for line in text.splitlines() if (m := _MODEL_ROW.match(line.strip("\r")))]
    return [ModelInfo(id=m.group(1), name=m.group(2), is_default=n == 0, takes_effort=True)
            for n, m in enumerate(rows)]


class AgyRuntime:
    name = support.AGENT
    contract_version = CONTRACT_VERSION

    def __init__(self, *, launcher: Launcher | None = None) -> None:
        self._launcher = launcher or _Executor()
        self._guards: dict[str | None, UsageGuard] = {}
        self._versions: dict[tuple[str, int], str] = {}
        self._models: dict[tuple[str, int, str | None], list[ModelInfo]] = {}
        self._failures: dict[str, tuple[Any, float, RuntimeFailure]] = {}
        self._probing: dict[tuple[str, int], asyncio.Future] = {}
        self._plugin_ok: set[tuple[str, int, str]] = set()
        self._usage_at: dict[str | None, float] = {}
        self._cli_version: str | None = None

    def binary(self) -> str:
        return find_binary()

    # ── what it offers ───────────────────────────────────────────────────

    def check(self, spec: SessionSpec) -> SupportReport:
        return support.check(spec)

    def features(self) -> FeatureList:
        return support.features(self._cli_version, auth_env=AUTH_VARS)

    async def describe(self) -> RuntimeInfo:
        binary = self.binary()
        self._cli_version = await self._version(binary)
        signed_in = await asyncio.to_thread(logged_in)
        account = await asyncio.to_thread(account_fingerprint) if signed_in else None
        place = await asyncio.to_thread(install.plugin_dir)
        return RuntimeInfo(cli_path=binary, cli_version=self._cli_version, logged_in=signed_in, account=account,
                           probes={"plugin": str(place), "auto": "XO's trusted auto: agy's own needs its sandbox "
                                                                 "and isn't offered to this account"})

    async def _run(self, argv: list[str], *, label: str, stdin: bytes | None = None,
                   timeout: float = PROBE_TIMEOUT_S) -> tuple[int, str]:
        """One model-free agy call in an empty folder: its exit and stdout."""
        env = await asyncio.to_thread(probe_env, label)
        with tempfile.TemporaryDirectory(prefix="xo-agy-probe-") as cwd:
            try:
                handle = await self._launcher.spawn(argv, cwd=cwd, env=env, label=label)
            except OSError as exc:
                raise RuntimeFailure("cli_missing", f"the agy CLI can't be started ({argv[0]}: "
                                                    f"{exc.strerror or exc}); install it, or point AGY_CLI_PATH "
                                                    "at an executable agy") from exc
            try:
                if stdin is not None:
                    await handle.write(stdin)
                await handle.close_stdin()

                async def collect() -> tuple[int, str]:
                    lines = [line async for line in handle.lines()]
                    return await handle.wait(), b"\n".join(lines).decode(errors="replace")
                return await asyncio.wait_for(collect(), timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeFailure("timeout", f"agy {' '.join(argv[1:3])} didn't finish", retryable=True) from exc
            finally:
                await handle.terminate(KILL_GRACE_S)

    async def _version(self, binary: str) -> str:
        key = (binary, await asyncio.to_thread(_mtime_ns, binary))
        if key not in self._versions:
            code, out = await self._run([binary, "--version"], label="agy probe")
            found = _VERSION.search(out) if code == 0 else None
            if found is None:
                raise RuntimeFailure("cli_missing", f"{binary} --version failed (exit {code})")
            self._versions = {key: found.group(1)}
        return self._versions[key]

    async def _once(self, what: str, key: Any, probe) -> Any:
        """One probe of a kind at a time, shared by those who ask meanwhile;
        a failure is kept for ``FAILURE_TTL_S``."""
        failure = self._failures.get(what)
        if failure is not None and failure[0] == key and time.monotonic() - failure[1] < FAILURE_TTL_S:
            raise RuntimeFailure(failure[2].code, f"{failure[2].message} (failed in the last {FAILURE_TTL_S:g} s; "
                                                  "not asked again yet)", retryable=True)
        # per event loop: ``/api/models`` runs its own in a worker thread
        slot = (what, id(asyncio.get_running_loop()))
        running = self._probing.get(slot)
        if running is not None:
            return await asyncio.shield(running)
        future = asyncio.ensure_future(probe())
        self._probing[slot] = future
        try:
            result = await future
        except RuntimeFailure as exc:
            self._failures[what] = (key, time.monotonic(), exc)
            raise
        finally:
            self._probing.pop(slot, None)
        self._failures.pop(what, None)
        return result

    async def models(self) -> list[ModelInfo]:
        """agy's own model list (``agy models``, model-free), kept per
        installed binary and account."""
        binary = self.binary()
        key = (binary, await asyncio.to_thread(_mtime_ns, binary), await asyncio.to_thread(account_fingerprint))
        if key in self._models:
            return list(self._models[key])

        async def probe() -> list[ModelInfo]:
            code, out = await self._run([binary, "models"], label="agy models probe")
            found = models_from(out)
            if code != 0 or not found:
                raise RuntimeFailure("auth_required" if code != 0 else "protocol",
                                     f"agy models listed nothing (exit {code}); is agy signed in?")
            return found
        found = await self._once("models", key, probe)
        self._models = {key: found}
        return list(found)

    async def refresh_usage(self, *, force: bool = False) -> UsageGuard:
        """agy's usage windows (``agy -p /usage``, model-free), read again
        once ``USAGE_TTL_S`` has passed."""
        account = await asyncio.to_thread(account_fingerprint)
        guard = self._guards.setdefault(account, UsageGuard())
        if not force and time.monotonic() - self._usage_at.get(account, -USAGE_TTL_S) < USAGE_TTL_S:
            return guard
        binary = self.binary()

        async def probe() -> None:
            code, out = await self._run([binary, "-p", "/usage"], label="agy usage probe")
            windows = parse_usage(out)
            if code != 0 or not windows:
                raise RuntimeFailure("protocol", f"agy's usage report was empty (exit {code})")
            guard.update(windows)
        await self._once("usage", account, probe)
        self._usage_at[account] = time.monotonic()
        return guard

    async def usage(self) -> UsageSnapshot:
        try:
            guard = await self.refresh_usage()
        except RuntimeFailure:
            guard = self._guards.setdefault(await asyncio.to_thread(account_fingerprint), UsageGuard())
        return guard.snapshot()

    async def check_plugin(self, digest: str) -> None:
        """agy lists XO's plugin, enabled, with its hook (``agy plugin list
        --json``, once per binary and plugin version). A plugin the person
        turned off is theirs to turn on again: XO doesn't."""
        binary = self.binary()
        key = (binary, await asyncio.to_thread(_mtime_ns, binary), digest)
        if key in self._plugin_ok:
            return
        try:
            code, out = await self._run([binary, "plugin", "list", "--json"], label="agy plugin probe")
            listed = json.loads(out[out.find("["):]) if "[" in out else None
        except (RuntimeFailure, ValueError):
            return                              # can't tell: the hook fails closed if it isn't there
        if code != 0 or not isinstance(listed, list):
            return
        mine = next((p for p in listed if isinstance(p, dict) and p.get("name") == install.PLUGIN_NAME), None)
        if mine is None:
            raise RuntimeFailure("spec_refused", f"agy doesn't list XO's plugin at {install.plugin_dir()}, so XO "
                                                 "can't check its actions; check agy's plugin settings")
        if mine.get("enabled") is False:
            raise RuntimeFailure("spec_refused", "XO's agy plugin is turned off, so XO can't check agy's actions; "
                                                 f"turn it on with `agy plugin enable {install.PLUGIN_NAME}`")
        self._plugin_ok.add(key)

    # ── sessions ─────────────────────────────────────────────────────────

    async def open(self, spec: SessionSpec, host: Host, *, session_id: str) -> AgySession:
        if not _SESSION_ID.fullmatch(session_id):
            raise RuntimeFailure("spec_refused", f"not a session id: {session_id!r} (letters, digits, _ and -)")
        report = self.check(spec)
        names = [s.name for s in spec.mcp_servers]

        async def prepare() -> None:
            """Before each agy process of the session: XO's plugin as XO
            wrote it (a byte compare when nothing changed), and listed."""
            try:
                digest = await asyncio.to_thread(install.ensure, names)
            except install.InstallFailed as exc:
                raise RuntimeFailure("spec_refused", str(exc)) from exc
            await self.check_plugin(digest)
        account = await asyncio.to_thread(account_fingerprint)
        guard = self._guards.setdefault(account, UsageGuard())
        binary = self.binary()
        try:
            self._cli_version = await self._version(binary)
        except RuntimeFailure:
            pass                                # the session's own start says what's wrong
        session = AgySession(session_id=session_id, spec=spec, report=report, host=host, binary=binary,
                             folder=socket_folder(run_dir(), session_id), guard=guard,
                             prepare=prepare, usage_refresh=self.refresh_usage)
        session.cli_version = self._cli_version
        await session.start()
        return session

    # ── agy's own files ──────────────────────────────────────────────────

    async def list_sessions(self, *, cwd: str | None = None, limit: int = 50) -> list[SessionInfo]:
        return await asyncio.to_thread(transcript.list_sessions, cwd=cwd, limit=limit)

    async def read_session(self, native_id: str, *, after: str | None = None, limit: int = 50) -> TranscriptPage:
        return await asyncio.to_thread(transcript.read, native_id, after=after, limit=limit)

    async def list_runs(self, native_id: str) -> list[RunInfo]:
        return await asyncio.to_thread(transcript.list_runs, native_id)

    async def read_run(self, native_id: str, run_id: str, *, after: str | None = None,
                       limit: int = 50) -> TranscriptPage:
        return await asyncio.to_thread(transcript.read_run, native_id, run_id, after=after, limit=limit)


def _mtime_ns(binary: str) -> int:
    path = shutil.which(binary) or binary
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return 0


_RUNTIME: AgyRuntime | None = None


def runtime() -> AgyRuntime:
    """The agy runtime: one per server, so its usage guards (one per
    account), model list and plugin check are shared by every session."""
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = AgyRuntime()
    return _RUNTIME
