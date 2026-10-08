"""
Codex Remote Control lifecycle: start / pair / stop / inspect the codex
app-server daemon so the ChatGPT app can drive this machine. Codex counterpart
of ``adapters/claude_code/remote_control.py``, with the same response contract
plus ``pair`` (the ChatGPT app pairs by short-lived code, not by link).

The CLI owns the daemon; this module runs the commands in ``_COMMANDS``
(codex-cli 0.152.x) and normalises their ``--json`` output. What the CLI does
not report (connection status, paired devices, whether a code was claimed) is
read from the daemon's control socket through ``app_server_rpc``.

``start`` and ``stop`` can outlast any HTTP request: ``codex remote-control
start`` may prepare the managed install, gracefully stop a running daemon
(60s grace by default) and wait for the daemon's own operation lock (up to
375s). They therefore run as background tasks: the request waits at most
``RESPONSE_WAIT_SECONDS`` and otherwise answers ``pending``, and ``status``
reports the action until it finishes.

Pairing codes are credentials: ``pair`` runs with ``log_output=False``, since
the command-log redaction cannot recognise them, and no error message carries
one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

from services.cowork_agent.providers_status_lib import codex_oauth_connected
from services.cowork_agent.registry.agent_registry import get_agent
from utils.commands import CommandResult, run

from . import app_server_rpc
from .paths import codex_home

logger = logging.getLogger(__name__)

AGENT = "codex"

# The CLI's wall clock for start / stop (CODEX_REMOTE_CONTROL_TIMEOUT; `pair`
# is capped at REQUEST_TIMEOUT_SECONDS).
# Long, because start and stop run in the background (see module docstring)
# and a kill mid-start can leave the daemon half restarted.
DEFAULT_ACTION_TIMEOUT_SECONDS = 180.0
# How long a start / stop request waits before answering `pending`: well under
# the workspace proxy's 60s request limit.
RESPONSE_WAIT_SECONDS = 20.0
# The whole budget of an action answered inside the request (`pair`): the wait
# for `_action_lock` plus the CLI run. Under the workspace proxy's 60s limit so
# a slow action returns `busy` / `timeout` instead of a bare 504.
REQUEST_TIMEOUT_SECONDS = 45.0
PROBE_TIMEOUT_SECONDS = 15.0
_MAX_DETAIL_CHARS = 400
_MAX_DEVICES = 50

# The ChatGPT app's own pairing QR encodes this URL with the raw pairingCode.
PAIRING_URL = "https://chatgpt.com/codex/pair"
PAIRING_INSTRUCTIONS = (
    "Scan the QR code with your phone to open the ChatGPT app. On a computer, open "
    "the Codex pairing screen, choose Pair manually, and enter the code. Each code "
    "works once."
)

# Server name / environment id from the last `start`; the fallback when the
# daemon's status read is unavailable.
_last_enrollment: Optional[dict[str, Any]] = None
# The start / stop running in the background, if any: (action, task).
_current_action: Optional[tuple[str, asyncio.Task]] = None
# How the most recent start / stop went (status reports it).
_last_action: Optional[dict[str, Any]] = None
# Serialise lifecycle actions within this process.
_action_lock = asyncio.Lock()


class RemoteControlError(Exception):
    """An expected failure, reported as ``{ok: False, error, detail}``.
    Neither ``message`` nor ``cli_output`` may ever carry a pairing code."""

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
    """The last line of ``output`` that parses as a JSON object (stdout and
    stderr are merged, so warnings may surround it), or ``None``."""
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
    """A failed CLI run's error text on one capped line, without JSON lines."""
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
    """The standalone installer's locations, for when PATH cannot see the CLI."""
    return [
        Path.home() / ".local" / "bin" / "codex",
        home / "packages" / "standalone" / "current" / "codex",
    ]


def resolve_binary() -> Optional[str]:
    """``CODEX_CLI_PATH`` → PATH → standalone locations; ``None`` when absent.
    An absolute ``CODEX_CLI_PATH`` is authoritative (no fallback)."""
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


def _request_timeout() -> float:
    """``_action_timeout()`` capped at ``REQUEST_TIMEOUT_SECONDS``: the env
    override may shorten an in-request action, never push it past the proxy."""
    return min(_action_timeout(), REQUEST_TIMEOUT_SECONDS)


async def _run_in_request(argv: list[str], *, log_output: bool = True) -> tuple[CommandResult, float]:
    """Run an action the request waits on, inside one deadline shared by the
    lock wait and the CLI. Returns the result and that budget (for the
    failure message); raises ``busy`` when the lock is not free in time."""
    budget = _request_timeout()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    try:
        await asyncio.wait_for(_action_lock.acquire(), timeout=budget)
    except asyncio.TimeoutError:
        raise RemoteControlError(
            "busy", "Another remote control action is still running. Try again when it finishes."
        ) from None
    try:
        timeout = max(deadline - loop.time(), 1.0)
        return await _run(argv, timeout=timeout, log_output=log_output), budget
    finally:
        _action_lock.release()


async def _run(argv: list[str], *, timeout: float, log_output: bool = True) -> CommandResult:
    # output_to_file: `remote-control start` forks the daemon, which can keep
    # an output pipe open long after the CLI exits.
    return await run(
        argv, cwd=get_agent(AGENT).cwd, timeout=timeout, log_output=log_output, output_to_file=True
    )


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


def _default_socket_path() -> Path:
    """Where the daemon listens (codex-rs ``app_server_control_socket_path``)."""
    return codex_home() / "app-server-control" / "app-server-control.sock"


def _read_json_file(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _live_pid() -> Optional[int]:
    """The daemon's pid (``daemon.pid``; ``app-server.pid`` before codex-cli
    0.160), only while that process exists."""
    record = (
        _read_json_file(_daemon_dir() / "daemon.pid")
        or _read_json_file(_daemon_dir() / "app-server.pid")
        or {}
    )
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


def _unix_to_iso(value: Any) -> Optional[str]:
    """Unix seconds (milliseconds tolerated) → ISO-8601 UTC, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if seconds > 1e12:
        seconds /= 1000.0
    try:
        stamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.isoformat().replace("+00:00", "Z")


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
    """Probe result → daemon view; a refused socket means stopped, not error."""
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
    return {"supported": True, "qr": True, "instructions": PAIRING_INSTRUCTIONS}


def _pending_action() -> Optional[str]:
    if _current_action is not None and not _current_action[1].done():
        return _current_action[0]
    return None


def _empty_remote_view() -> dict[str, Any]:
    return {"connection": None, "devices": None, "remote_error": None}


def _status_view(
    daemon: dict[str, Any],
    enrollment: Optional[dict[str, Any]],
    cli: dict[str, Any],
    remote: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """The claude_code-compatible status keys plus the codex-specific detail."""
    remote = remote or _empty_remote_view()
    connection = remote.get("connection") or {}
    return {
        "running": bool(daemon.get("running")),
        "login_present": codex_oauth_connected(),
        "session_url": None,  # codex pairs by code, not by link
        "pid": daemon.get("pid"),
        "name": connection.get("server_name") or (enrollment or {}).get("server_name"),
        "agent": AGENT,
        "checked_at": _now_iso(),
        "cli": cli,
        "daemon": daemon,
        "enrollment": dict(enrollment) if enrollment else None,
        # Live from the daemon; None when it is down or could not be asked.
        "connection": remote.get("connection"),
        "devices": remote.get("devices"),
        "remote_error": remote.get("remote_error"),
        "pending_action": _pending_action(),
        "last_action": dict(_last_action) if _last_action else None,
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
    stamp = _unix_to_iso(expires_at)
    if stamp is None:
        return None, None
    seconds = float(expires_at)
    if seconds > 1e12:
        seconds /= 1000.0
    return stamp, max(0, int(seconds - time.time()))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _pairing_url(pairing_code: str) -> Optional[str]:
    """The URL the ChatGPT app's own pairing QR encodes, for the raw code."""
    return f"{PAIRING_URL}?pairing_code={quote(pairing_code, safe='')}" if pairing_code else None


# ── Daemon RPC views ─────────────────────────────────────────────────────

def _socket_path(daemon: Optional[dict[str, Any]] = None) -> Path:
    reported = (daemon or {}).get("socket_path")
    return Path(reported) if isinstance(reported, str) and reported else _default_socket_path()


def _device_view(client: dict[str, Any]) -> dict[str, Any]:
    return {
        "client_id": _text(client.get("clientId")) or None,
        "display_name": _text(client.get("displayName")) or None,
        "device_type": _text(client.get("deviceType")) or None,
        "platform": _text(client.get("platform")) or None,
        "os_version": _text(client.get("osVersion")) or None,
        "device_model": _text(client.get("deviceModel")) or None,
        "app_version": _text(client.get("appVersion")) or None,
        "last_seen_at": _unix_to_iso(client.get("lastSeenAt")),
    }


async def _remote_view(daemon: dict[str, Any]) -> dict[str, Any]:
    """Connection status and paired devices, read from the running daemon.
    Best-effort: these methods are experimental upstream."""
    view = _empty_remote_view()
    socket_path = _socket_path(daemon)
    try:
        status = await app_server_rpc.call(socket_path, "remoteControl/status/read")
    except app_server_rpc.AppServerRpcError as exc:
        view["remote_error"] = str(exc)
        return view
    environment_id = _text(status.get("environmentId")) or None
    view["connection"] = {
        "status": _text(status.get("status")) or None,
        "server_name": _text(status.get("serverName")) or None,
        "environment_id": environment_id,
    }
    if environment_id is None:
        return view  # not enrolled yet: nothing can be paired
    try:
        listing = await app_server_rpc.call(
            socket_path,
            "remoteControl/client/list",
            {"environmentId": environment_id, "limit": _MAX_DEVICES, "order": "desc"},
        )
    except app_server_rpc.AppServerRpcError as exc:
        view["remote_error"] = str(exc)
        return view
    clients = listing.get("data")
    view["devices"] = [
        _device_view(client) for client in (clients if isinstance(clients, list) else [])
        if isinstance(client, dict)
    ]
    return view


# ── Background start / stop ──────────────────────────────────────────────

_PROGRESSIVE = {"start": "starting", "stop": "stopping"}


def _record_action(action: str, state: str, response: Optional[dict[str, Any]] = None) -> None:
    global _last_action
    if state == "running":
        _last_action = {
            "action": action, "state": "running", "started_at": _now_iso(),
            "finished_at": None, "error": None, "detail": None,
        }
        return
    record = dict(_last_action or {"action": action, "started_at": None})
    record.update({
        "state": state,
        "finished_at": _now_iso(),
        "error": (response or {}).get("error"),
        "detail": (response or {}).get("detail") if state == "failed" else (response or {}).get("message"),
    })
    _last_action = record


async def _tracked(action: str, work: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    _record_action(action, "running")
    try:
        response = await work()
    except Exception:
        # The request may be long gone; this is the only place it surfaces.
        logger.exception("codex remote-control %s failed unexpectedly", action)
        response = RemoteControlError(
            "internal_error", f"Remote control {action} failed unexpectedly; see the server log."
        ).as_response()
    _record_action(action, "succeeded" if response.get("ok") else "failed", response)
    return response


async def _in_background(action: str, work: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Run ``work`` as a task that outlives the request; answer with its
    result when it finishes within ``RESPONSE_WAIT_SECONDS``, else ``pending``.
    A repeat of the running action joins it instead of queueing another."""
    global _current_action
    pending = _pending_action()
    if pending is not None and pending != action:
        return RemoteControlError(
            "busy", f"Remote control is still {_PROGRESSIVE[pending]}. Try again when it finishes."
        ).as_response()
    if pending is None:
        _current_action = (action, asyncio.create_task(_tracked(action, work)))
    task = _current_action[1]
    try:
        # shield: a dropped request must not cancel the CLI half way.
        return await asyncio.wait_for(asyncio.shield(task), timeout=RESPONSE_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return {
            "ok": True,
            "pending": True,
            "action": action,
            "message": f"Remote control is still {_PROGRESSIVE[action]}. This can take a few minutes.",
        }


# ── Public API (status / start / pair / pairing_status / stop) ───────────

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
    remote = None
    if daemon["running"]:
        remote = await _remote_view(daemon)
    elif _pending_action() is None:
        _last_enrollment = None
    return _status_view(
        daemon,
        _last_enrollment,
        {"available": True, "path": binary, "version": _cli_version(version_result)},
        remote,
    )


async def start(name: Optional[str] = None) -> dict[str, Any]:
    """Start the daemon (idempotent), in the background past
    ``RESPONSE_WAIT_SECONDS``. ``name`` is accepted for parity with
    claude_code and ignored: codex names the server itself."""
    return await _in_background("start", _start)


async def _start() -> dict[str, Any]:
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
    """A fresh single-use pairing code and its QR URL; needs the daemon running."""
    try:
        pending = _pending_action()
        if pending is not None:
            raise RemoteControlError(
                "busy", f"Remote control is still {_PROGRESSIVE[pending]}. Try again when it finishes."
            )
        binary = _require_binary()
        # The output carries the code: keep it out of commands.log.
        result, budget = await _run_in_request(_argv("remote_control_pair", binary), log_output=False)
        _raise_for_failure(result, "remote-control pair", budget)
        payload = parse_json_object(result.output) or {}
        manual_code = _text(payload.get("manualPairingCode"))
        raw_code = _text(payload.get("pairingCode"))
        code = manual_code or raw_code
        if not code:
            # No CLI output attached: it may carry a code.
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
        "pairing_url": _pairing_url(raw_code),
        "environment_id": _text(payload.get("environmentId")) or None,
        "expires_at": expires_at,
        "expires_in_seconds": expires_in,
        "instructions": PAIRING_INSTRUCTIONS,
    }


async def pairing_status(
    *, pairing_code: Optional[str] = None, manual_pairing_code: Optional[str] = None
) -> dict[str, Any]:
    """Whether a code from ``pair`` has been claimed by a device. Exactly one
    of the two codes; the raw code is preferred when both are given."""
    raw_code, manual_code = _text(pairing_code), _text(manual_pairing_code)
    if raw_code:
        params = {"pairingCode": raw_code}
    elif manual_code:
        params = {"manualPairingCode": manual_code}
    else:
        return RemoteControlError(
            "bad_request", "Pass the pairing_code or manual_pairing_code to check."
        ).as_response()
    try:
        result = await app_server_rpc.call(_socket_path(), "remoteControl/pairing/status", params)
    except app_server_rpc.AppServerRpcError:
        # The daemon's error text is not ours to vet for the code: keep it out.
        return RemoteControlError(
            "unavailable", "Could not check the pairing code with the codex daemon."
        ).as_response()
    return {"ok": True, "claimed": result.get("claimed") is True}


async def stop() -> dict[str, Any]:
    """``codex remote-control stop --json`` (idempotent), in the background
    past ``RESPONSE_WAIT_SECONDS``."""
    return await _in_background("stop", _stop)


async def _stop() -> dict[str, Any]:
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
    "PAIRING_URL",
    "RemoteControlError",
    "collapse_cli_error",
    "get_status",
    "pair",
    "pairing_status",
    "parse_json_object",
    "resolve_binary",
    "start",
    "stop",
]
