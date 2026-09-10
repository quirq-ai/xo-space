"""
Codex Remote Control lifecycle.

Start / pair / stop / inspect the codex app-server daemon in remote-control
mode so this machine can be driven from the ChatGPT app — the codex
counterpart of ``adapters/claude_code/remote_control.py``. Served by
``adapters/codex/routes.py`` at the same ``/api/remote-control/*`` paths
(mounted only while codex is the active agent) plus ``pair``, which codex
needs because the ChatGPT app pairs by short-lived code, not by link.

The CLI owns the daemon (pid file, socket, enrollment); this module only
drives it and normalises its ``--json`` output:

    codex remote-control start --json    start the daemon with remote control
                                         enabled (idempotent; returns once up)
    codex remote-control pair  --json    mint a single-use manual pairing code
    codex remote-control stop  --json    stop the daemon (idempotent)
    codex app-server daemon version      read-only "is it running" probe
    codex --version                      CLI version for the status card

Argv shapes are fixed in ``_COMMANDS`` below (the codex adapter hardcodes its
own CLI shapes the same way); only the binary is resolved
(``CODEX_CLI_PATH`` → PATH → the standalone installer's known locations).

Verified against codex-cli 0.152.x on 2026-09-02:

    start   → {"mode":"daemon","status":"connected"|"connecting"|"errored"|"disabled",
               "serverName":…,"environmentId":"env_…","timedOut":false,
               "daemon":{"status":"bootstrapped"|"alreadyRunning","backend":"pid",
               "remoteControlEnabled":true,"managedCodexPath":…,"managedCodexVersion":…,
               "socketPath":…,"cliVersion":…,"appServerVersion":…}}      exit 0 both times
    pair    → {"pairingCode":…,"manualPairingCode":…,"environmentId":…,
               "expiresAt":<unix seconds, about ten minutes out>}
              exit 1 + "failed to connect to …/app-server-control.sock" when the
              daemon is down — it does NOT start one
    stop    → {"status":"stopped"|"notRunning",…}                          exit 0
    version → {"status":"running",…,"appServerVersion":…}, or exit 1 with the
              same "failed to connect" text when stopped

Response contract mirrors claude_code's so the frontend's one Remote Control
button reads both: ``running`` / ``login_present`` / ``session_url`` (always
None here — codex has no deep link) / ``pid`` / ``name`` on status, ``ok`` +
``already_running`` on start, ``ok`` on stop, and ``{ok: False, error, detail}``
(HTTP 200) for expected failures. Codex adds ``cli``, ``daemon``,
``enrollment`` and ``pairing``.

Pairing codes are credentials: returned to the caller once, never logged
(``utils.commands.run`` writes no log unless asked; nothing here asks).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.cowork_agent.providers_status_lib import codex_oauth_connected
from services.cowork_agent.registry.agent_registry import get_agent
from utils.commands import CommandResult, run

from .paths import codex_home

AGENT = "codex"

# `start` enrolls with the remote-control service and boots the daemon, so it
# gets the long budget (CODEX_REMOTE_CONTROL_TIMEOUT overrides it). Probes
# stay short because frontends poll status on every load.
DEFAULT_ACTION_TIMEOUT_SECONDS = 90.0
PROBE_TIMEOUT_SECONDS = 15.0
_MAX_DETAIL_CHARS = 400

PAIRING_INSTRUCTIONS = (
    "In the ChatGPT app, open the Codex pairing screen, choose Pair manually, "
    "and enter this code before it expires. Each code works once."
)

# The last successful `start` in this process: the only place the server
# name and environment id are reported, so status keeps them while the
# daemon stays up. Cleared by `stop` or when a probe finds the daemon gone.
_last_enrollment: Optional[dict[str, Any]] = None
# Serialise lifecycle actions within this process; the CLI has its own
# startup lock, but two overlapping starts would still confuse the caller.
_action_lock = asyncio.Lock()


class RemoteControlError(Exception):
    """An expected failure, reported to callers as ``{ok: False, error, detail}``.

    ``code``: ``cli_missing`` | ``unsupported_platform`` | ``daemon_not_running``
    | ``timeout`` | ``bad_output`` | ``cli_error``. ``message`` is safe to show;
    ``cli_output`` is the CLI's own text. Neither may ever carry a pairing code.
    """

    def __init__(self, code: str, message: str, *, cli_output: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.cli_output = cli_output

    def as_response(self) -> dict[str, Any]:
        response: dict[str, Any] = {"ok": False, "error": self.code, "detail": self.message}
        if self.cli_output:
            response["cli_output"] = self.cli_output
        if self.code in ("cli_missing", "daemon_not_running"):
            response["running"] = False
        return response


# ── CLI output helpers ───────────────────────────────────────────────────

def parse_json_object(output: str) -> Optional[dict[str, Any]]:
    """The last line of ``output`` that parses as a JSON object, or ``None``.

    ``utils.commands.run`` merges stdout and stderr, so the ``--json`` payload
    may sit among warnings. The CLI prints exactly one object per run; taking
    the last parseable line keeps a stray earlier brace from winning.
    """
    found: Optional[dict[str, Any]] = None
    for raw in output.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            found = parsed
    return found


def collapse_cli_error(output: str, *, fallback: str) -> str:
    """One line of readable error text from a failed CLI run: JSON lines
    dropped, the ``Error:`` prefix stripped, an anyhow ``Caused by:`` block
    folded onto the same line, and the length capped."""
    lines = []
    for raw in output.splitlines():
        line = raw.strip()
        if line and not line.startswith("{"):
            lines.append(line)
    text = " ".join(lines)
    text = re.sub(r"^Error:\s*", "", text)
    text = re.sub(r"\s*Caused by:\s*", " (caused by: ", text, count=1)
    if "(caused by: " in text:
        text += ")"
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return fallback
    if len(text) > _MAX_DETAIL_CHARS:
        text = text[: _MAX_DETAIL_CHARS - 1] + "…"
    return text


# ── Binary + argv ────────────────────────────────────────────────────────

def _standalone_candidates(home: Path) -> list[Path]:
    """Where the CLI lives when the server's PATH cannot see it: the
    standalone installer links ``~/.local/bin/codex`` (absent from a
    systemd- or coder-launched PATH) and keeps the real binary under
    ``$CODEX_HOME/packages/standalone/current/``."""
    return [
        Path.home() / ".local" / "bin" / "codex",
        home / "packages" / "standalone" / "current" / "codex",
    ]


def resolve_binary() -> Optional[str]:
    """``CODEX_CLI_PATH`` → PATH → standalone locations; ``None`` when absent.

    An absolute ``CODEX_CLI_PATH`` is authoritative (missing file → None, no
    silent fallback to a different install). A bare name that PATH cannot
    resolve behaves as if unset.
    """
    configured = (os.getenv("CODEX_CLI_PATH") or "").strip()
    if configured and os.path.isabs(configured):
        return configured if os.path.isfile(configured) else None
    for name in (configured, get_agent(AGENT).binary):
        if name:
            found = shutil.which(name)
            if found:
                return found
    for candidate in _standalone_candidates(codex_home()):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _require_binary() -> str:
    binary = resolve_binary()
    if binary is None:
        raise RemoteControlError(
            "cli_missing",
            "The codex CLI is not installed or not on the server's PATH. "
            "Install it, or point CODEX_CLI_PATH at the binary.",
        )
    return binary


# Arguments after the binary, per action (codex-cli 0.152.x).
_COMMANDS: dict[str, list[str]] = {
    "cli_version": ["--version"],
    "daemon_version": ["app-server", "daemon", "version"],
    "remote_control_start": ["remote-control", "start", "--json"],
    "remote_control_pair": ["remote-control", "pair", "--json"],
    "remote_control_stop": ["remote-control", "stop", "--json"],
}


def _argv(command: str, binary: str) -> list[str]:
    return [binary, *_COMMANDS[command]]


def _action_timeout() -> float:
    raw = (os.getenv("CODEX_REMOTE_CONTROL_TIMEOUT") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_ACTION_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_ACTION_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_ACTION_TIMEOUT_SECONDS


async def _run(argv: list[str], *, timeout: float) -> CommandResult:
    return await run(argv, cwd=get_agent(AGENT).cwd, timeout=timeout)


# ── Failure classification ───────────────────────────────────────────────

def _is_daemon_down(message: str) -> bool:
    return "failed to connect to" in message and ".sock" in message


def _raise_for_failure(result: CommandResult, action: str, timeout: float) -> None:
    if result.binary_missing:
        raise RemoteControlError(
            "cli_missing", f"The codex CLI could not be executed ({result.argv[0]})."
        )
    if result.timed_out:
        raise RemoteControlError(
            "timeout", f"`codex {action}` did not finish within {timeout:g}s."
        )
    if result.exception is not None:
        raise RemoteControlError(
            "cli_error", f"`codex {action}` could not be run: {result.exception}"
        )
    if result.returncode == 0:
        return
    message = collapse_cli_error(
        result.output, fallback=f"`codex {action}` exited with status {result.returncode}."
    )
    if _is_daemon_down(message):
        raise RemoteControlError(
            "daemon_not_running",
            "Remote control is not running on this machine. Start it first.",
            cli_output=message,
        )
    if "only supported on unix" in message.lower():
        raise RemoteControlError(
            "unsupported_platform",
            "The codex daemon lifecycle is only supported on Unix platforms.",
            cli_output=message,
        )
    raise RemoteControlError("cli_error", message)


# ── On-disk daemon facts ─────────────────────────────────────────────────

def _daemon_dir() -> Path:
    return codex_home() / "app-server-daemon"


def _read_json_file(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _live_pid() -> Optional[int]:
    """The pid recorded in ``app-server.pid``, only while that process exists.
    The CLI leaves a stale file behind after a crash, so the file alone is
    never taken as proof of life."""
    record = _read_json_file(_daemon_dir() / "app-server.pid") or {}
    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # alive, owned by someone else
    except OSError:
        return None
    return pid


def _remote_control_preference() -> Optional[bool]:
    """The daemon's persisted ``remoteControlEnabled`` setting, if written."""
    record = _read_json_file(_daemon_dir() / "settings.json") or {}
    value = record.get("remoteControlEnabled")
    return value if isinstance(value, bool) else None


# ── Views ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _daemon_view(payload: Optional[dict[str, Any]], *, running: bool) -> dict[str, Any]:
    payload = payload or {}
    enabled = payload.get("remoteControlEnabled")
    return {
        "status": "running" if running else "stopped",
        "running": running,
        "pid": _live_pid() if running else None,
        "app_server_version": payload.get("appServerVersion"),
        "cli_version": payload.get("cliVersion"),
        "managed_codex_version": payload.get("managedCodexVersion"),
        "socket_path": payload.get("socketPath"),
        "remote_control_enabled": enabled if isinstance(enabled, bool) else _remote_control_preference(),
        "error": None,
    }


def _unknown_daemon(reason: str) -> dict[str, Any]:
    view = _daemon_view(None, running=False)
    view.update({"status": "unknown", "error": reason})
    return view


def _interpret_probe(result: CommandResult) -> dict[str, Any]:
    """``codex app-server daemon version`` → daemon view. A refused socket
    connection is the normal "stopped" answer, not an error."""
    if result.ok:
        payload = parse_json_object(result.output) or {}
        return _daemon_view(payload, running=payload.get("status") == "running")
    if result.timed_out:
        return _unknown_daemon("The daemon status probe timed out.")
    if result.binary_missing or result.exception is not None:
        return _unknown_daemon(f"The daemon status probe could not run ({result.output.strip()}).")
    message = collapse_cli_error(result.output, fallback="The daemon status probe failed.")
    if _is_daemon_down(message):
        return _daemon_view(None, running=False)
    return _unknown_daemon(message)


def _cli_version(result: CommandResult) -> Optional[str]:
    """``codex --version`` prints ``codex-cli 0.152.0``; keep the number."""
    if not result.ok:
        return None
    lines = result.output.strip().splitlines()
    if not lines:
        return None
    parts = lines[-1].split()
    return parts[-1] if parts else None


def _pairing_view() -> dict[str, Any]:
    return {"supported": True, "instructions": PAIRING_INSTRUCTIONS}


def _status_view(
    daemon: dict[str, Any],
    enrollment: Optional[dict[str, Any]],
    cli: dict[str, Any],
) -> dict[str, Any]:
    """The claude_code-compatible status keys plus the codex-specific detail."""
    return {
        "running": bool(daemon.get("running")),
        "login_present": codex_oauth_connected(),
        "session_url": None,  # codex pairs by code, not by link
        "pid": daemon.get("pid"),
        "name": (enrollment or {}).get("server_name"),
        "agent": AGENT,
        "checked_at": _now_iso(),
        "cli": cli,
        "daemon": daemon,
        "enrollment": dict(enrollment) if enrollment else None,
        "pairing": _pairing_view(),
        "install_url": get_agent(AGENT).raw.get("install_url"),
    }


def _start_message(connection: str, server_name: Optional[str]) -> str:
    """Mirror the CLI's own wording so shell and API users read the same thing."""
    name = server_name or "this machine"
    if connection == "connected":
        return f"This machine is available for remote control as {name}."
    if connection == "connecting":
        return f"Remote control is enabled on {name} and still connecting."
    if connection == "errored":
        return f"Remote control is enabled on {name} but the connection is errored."
    if connection == "disabled":
        return f"Remote control is disabled on {name}."
    return f"Remote control daemon started ({connection})."


def _expiry(expires_at: Any) -> tuple[Optional[str], Optional[int]]:
    """``expiresAt`` (unix seconds; milliseconds tolerated) → ISO-8601 UTC and
    the whole seconds left from now, floored at zero."""
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return None, None
    seconds = float(expires_at)
    if seconds > 1e12:
        seconds /= 1000.0
    try:
        stamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None, None
    return stamp.isoformat().replace("+00:00", "Z"), max(0, int(seconds - time.time()))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# ── Public API (status / start / pair / stop) ────────────────────────────

async def get_status() -> dict[str, Any]:
    """Read-only: never starts, stops, or pairs anything."""
    global _last_enrollment
    binary = resolve_binary()
    if binary is None:
        _last_enrollment = None
        return _status_view(
            _unknown_daemon("The codex CLI is not installed or not on the server's PATH."),
            None,
            {"available": False, "path": None, "version": None},
        )
    version_result, probe_result = await asyncio.gather(
        _run(_argv("cli_version", binary), timeout=PROBE_TIMEOUT_SECONDS),
        _run(_argv("daemon_version", binary), timeout=PROBE_TIMEOUT_SECONDS),
    )
    daemon = _interpret_probe(probe_result)
    if not daemon["running"]:
        _last_enrollment = None
    return _status_view(
        daemon,
        _last_enrollment,
        {"available": True, "path": binary, "version": _cli_version(version_result)},
    )


async def start(name: Optional[str] = None) -> dict[str, Any]:
    """``codex remote-control start --json`` (idempotent; returns once the
    daemon is up and enrolled, or says why the connection is not).

    ``name`` is accepted for parity with claude_code's ``start`` and ignored:
    codex labels the server after the host, and the label comes back as
    ``name`` / ``enrollment.server_name``.
    """
    global _last_enrollment
    try:
        binary = _require_binary()
        timeout = _action_timeout()
        async with _action_lock:
            result = await _run(_argv("remote_control_start", binary), timeout=timeout)
        _raise_for_failure(result, "remote-control start", timeout)
        payload = parse_json_object(result.output)
        if payload is None:
            raise RemoteControlError(
                "bad_output",
                "`codex remote-control start` returned no status; run it in a shell to see why.",
                cli_output=collapse_cli_error(result.output, fallback="") or None,
            )
    except RemoteControlError as exc:
        return exc.as_response()

    daemon_payload = payload.get("daemon")
    daemon_payload = daemon_payload if isinstance(daemon_payload, dict) else {}
    connection = _text(payload.get("status")) or "unknown"
    server_name = _text(payload.get("serverName")) or None
    enrollment = {
        "server_name": server_name,
        "environment_id": _text(payload.get("environmentId")) or None,
        "connection_status": connection,
        "timed_out": bool(payload.get("timedOut")),
        "started_at": _now_iso(),
    }
    _last_enrollment = enrollment
    daemon = _daemon_view(daemon_payload, running=True)
    cli = {"available": True, "path": binary, "version": daemon.get("cli_version")}
    return {
        "ok": True,
        "already_running": daemon_payload.get("status") == "alreadyRunning",
        "message": _start_message(connection, server_name),
        **_status_view(daemon, enrollment, cli),
    }


async def pair() -> dict[str, Any]:
    """``codex remote-control pair --json``: a fresh single-use code. Needs the
    daemon running (``{ok: False, error: "daemon_not_running"}`` otherwise)."""
    try:
        binary = _require_binary()
        timeout = _action_timeout()
        async with _action_lock:
            result = await _run(_argv("remote_control_pair", binary), timeout=timeout)
        _raise_for_failure(result, "remote-control pair", timeout)
        payload = parse_json_object(result.output) or {}
        manual_code = _text(payload.get("manualPairingCode"))
        raw_code = _text(payload.get("pairingCode"))
        code = manual_code or raw_code
        if not code:
            # Deliberately no CLI output attached: nothing here may echo a code.
            raise RemoteControlError(
                "bad_output", "codex did not return a pairing code; try again."
            )
    except RemoteControlError as exc:
        return exc.as_response()

    expires_at, expires_in = _expiry(payload.get("expiresAt"))
    return {
        "ok": True,
        "manual_pairing_code": code,
        "pairing_code": raw_code or code,
        "environment_id": _text(payload.get("environmentId")) or None,
        "expires_at": expires_at,
        "expires_in_seconds": expires_in,
        "instructions": PAIRING_INSTRUCTIONS,
    }


async def stop() -> dict[str, Any]:
    """``codex remote-control stop --json`` (idempotent)."""
    global _last_enrollment
    try:
        binary = _require_binary()
        timeout = _action_timeout()
        async with _action_lock:
            result = await _run(_argv("remote_control_stop", binary), timeout=timeout)
        _raise_for_failure(result, "remote-control stop", timeout)
    except RemoteControlError as exc:
        return exc.as_response()

    payload = parse_json_object(result.output) or {}
    raw_status = _text(payload.get("status")) or "unknown"
    _last_enrollment = None
    if raw_status == "stopped":
        message = "Remote control stopped."
    elif raw_status == "notRunning":
        message = "Remote control was not running."
    else:
        message = f"Remote control stop completed with status {raw_status}."
    return {
        "ok": True,
        "running": False,
        "was_running": raw_status == "stopped",
        "raw_status": raw_status,
        "message": message,
        "daemon": _daemon_view(payload, running=False),
    }


__all__ = [
    "PAIRING_INSTRUCTIONS",
    "RemoteControlError",
    "collapse_cli_error",
    "get_status",
    "pair",
    "parse_json_object",
    "resolve_binary",
    "start",
    "stop",
]
