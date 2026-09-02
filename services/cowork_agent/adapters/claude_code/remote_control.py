"""claude_code Remote Control lifecycle.

Start / stop / inspect a local ``claude remote-control`` server so the
workspace's Claude Code session can be driven from the Claude mobile app or
claude.ai/code. The session runs *here* (this workspace's filesystem); the
phone/browser is only a window into it.

Design notes (see docs/superpowers/specs/2026-07-22-claude-remote-control-design.md):
  * cwd is XO_RC_DIR (default: the user's HOME), so the session can reach
    everything under it. Trust for that dir is seeded in ~/.claude.json.
  * Auth: native login only. RC rejects CLAUDE_CODE_OAUTH_TOKEN / API keys, so we
    always strip the three token vars and require ~/.claude/.credentials.json.
  * Two non-interactive gates (trust + enable dialog) are pre-seeded here and at boot.
  * Detached process tracked by a PID file; outlives API restarts; stop() kills the group.
  * start()/stop() are serialized by a file lock so concurrent calls can't
    double-spawn (idempotency holds even under simultaneous requests).
  * We do NOT record the CLI's live dashboard — it repaints ~6 lines/sec (~1.3 MB/hr
    if captured to a file). Instead we launch through a filter that extracts the
    connect link into URL_FILE (one line, always current) and discards the rest.

Note: this builds on undocumented CLI internals (the ~/.claude.json gate keys and
parsing the dashboard for the link) because `claude remote-control` has no headless
API. Pin the claude CLI version and run a start→url→stop smoke test on upgrades.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from services.cowork_agent.adapters.cli_status import resolve_binary
from services.cowork_agent.adapters.claude_code.auth_state import record_auth_failure

logger = logging.getLogger(__name__)

PID_FILE = Path("/tmp/xo-rc.pid")
URL_FILE = Path("/tmp/xo-rc.url")   # single line: the current connect link
ERR_FILE = Path("/tmp/xo-rc.err")   # small: the CLI's stderr, for debugging a failed start
NAME_FILE = Path("/tmp/xo-rc.name")  # the label the session was launched with
LOCK_FILE = Path("/tmp/xo-rc.lock")  # serializes start/stop
_STARTUP_WAIT_SECONDS = 2.0
_STARTUP_POLL_SECONDS = 0.1
# Files removed on stop / a failed start (LOCK_FILE is persistent).
_STATE_FILES = (PID_FILE, URL_FILE, ERR_FILE, NAME_FILE)

# Auth vars Remote Control can never use — always dropped so the CLI falls back
# to the native ~/.claude/.credentials.json login.
_TOKEN_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")

_CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
_GLOBAL_CONFIG = Path(os.path.expanduser("~/.claude.json"))

# Launch through a filter so we never hoard the CLI's live dashboard. stdout is
# streamed through grep, which extracts the stable "environment" connect link into
# $RC_URL (overwritten each frame → always one current line) and drops everything
# else; stderr (real errors, low volume) is truncated into $RC_ERR each run.
# To surface per-session links instead of the environment link, change the grep
# pattern to: https://claude\.ai/code/session_[A-Za-z0-9]+
_LAUNCH_SCRIPT = r'''
"$RC_BIN" remote-control --name "$RC_NAME" 2>"$RC_ERR" \
| grep --line-buffered -aoE 'https://claude\.ai/code\?environment=env_[A-Za-z0-9]+' \
| while IFS= read -r u; do printf '%s\n' "$u" > "$RC_URL"; done
'''


def _launch_dir() -> Path:
    """Directory the Remote Control session starts in.

    Defaults to the user's home (``~``) so the session can reach everything under
    it. Override with the ``XO_RC_DIR`` env var (set it in .env). If the override
    is not an existing directory, we fall back to home. Trust for the resulting
    dir is seeded in ~/.claude.json and inherits to child dirs."""
    raw = (os.getenv("XO_RC_DIR", "") or "").strip() or "~"
    d = Path(raw).expanduser()
    if not d.is_dir():
        d = Path.home()
    return d.resolve()


@contextmanager
def _lock() -> Iterator[None]:
    """Exclusive lock serializing start/stop. flock on a dedicated file holds
    across threads (FastAPI's sync-handler threadpool) and processes (multiple
    uvicorn workers), so the check-then-spawn in start() can't race itself."""
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


# ── process helpers ──────────────────────────────────────────────────────────

def _read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` is a live process. Reaps it if it's our own zombie child
    (crash-exit while we were the parent); tolerates non-child pids, so a session
    started by a previous API instance and re-adopted by init still reads as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False  # was a zombie of ours — now reaped
    except ChildProcessError:
        pass  # not our child (e.g. after an API restart) — but it is alive
    except OSError:
        pass
    return True


def _running_pid() -> Optional[int]:
    pid = _read_pid()
    if pid is not None and _pid_alive(pid):
        return pid
    return None


def _session_url() -> Optional[str]:
    """The current connect link, captured by the launch filter into URL_FILE."""
    try:
        url = URL_FILE.read_text().strip()
    except OSError:
        return None
    return url or None


def _last_error() -> Optional[str]:
    """The final CLI error, if Remote Control failed to start or later stopped."""
    try:
        detail = ERR_FILE.read_text(errors="replace").strip()
    except OSError:
        return None
    return detail or None


def _wait_for_start() -> bool:
    """Wait briefly for a link or an early launcher exit.

    A live process without a link is still a successful asynchronous launch: the
    CLI may need longer than this small window to render its first dashboard.
    """
    deadline = time.monotonic() + _STARTUP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _session_url() is not None:
            return True
        if _running_pid() is None:
            return False
        time.sleep(_STARTUP_POLL_SECONDS)
    return _running_pid() is not None


def _cleanup_state(*, keep_error: bool = False) -> None:
    for f in _STATE_FILES:
        if keep_error and f == ERR_FILE:
            continue
        try:
            f.unlink()
        except OSError:
            pass


# ── auth / gate helpers ──────────────────────────────────────────────────────

def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in _TOKEN_VARS:
        env.pop(key, None)
    return env


def native_login_present() -> bool:
    """True when a native claude.ai OAuth session exists — the only credential
    Remote Control accepts. Written by the 'Connect Claude' (`claude auth login`)
    flow, self-refreshing thereafter."""
    for name in (".credentials.json", "credentials.json"):
        p = _CLAUDE_HOME / name
        try:
            if p.is_file() and p.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def ensure_gates_seeded() -> None:
    """Idempotently pre-clear the trust + enable dialogs in ~/.claude.json.

    Seeds trust for the launch dir. Skips the write when both are already set (the
    steady state after ``setup.sh`` seeds them at boot), so we don't race the CLI,
    which rewrites this file on exit. Only writes when something is missing, atomically.
    """
    launch_dir = str(_launch_dir())
    try:
        data = json.loads(_GLOBAL_CONFIG.read_text()) if _GLOBAL_CONFIG.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    projects = data.get("projects")
    proj_entry = projects.get(launch_dir) if isinstance(projects, dict) else None
    trust_ok = isinstance(proj_entry, dict) and proj_entry.get("hasTrustDialogAccepted") is True
    dialog_ok = data.get("remoteDialogSeen") is True
    if trust_ok and dialog_ok:
        return  # already seeded — no write, no race

    data["remoteDialogSeen"] = True
    if not isinstance(data.get("projects"), dict):
        data["projects"] = {}
    entry = data["projects"].get(launch_dir)
    if not isinstance(entry, dict):
        entry = {}
    entry["hasTrustDialogAccepted"] = True
    data["projects"][launch_dir] = entry

    tmp = tempfile.NamedTemporaryFile(
        "w", dir=str(_GLOBAL_CONFIG.parent), delete=False, suffix=".xo-rc.tmp"
    )
    try:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, _GLOBAL_CONFIG)
    except OSError:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


# ── public API (start / stop / status) ───────────────────────────────────────

def _default_name() -> str:
    return (os.getenv("XO_WORKSPACE_NAME", "") or "").strip() or socket.gethostname()


def _session_name() -> str:
    """The label the running session was launched with (persisted at start)."""
    try:
        n = NAME_FILE.read_text().strip()
    except OSError:
        n = ""
    return n or _default_name()


def status() -> dict[str, Any]:
    login = native_login_present()
    pid = _running_pid()
    if pid is None:
        detail = _last_error()
        if detail:
            record_auth_failure(detail)
        return {
            "running": False,
            "login_present": login,
            "session_url": None,
            "last_error": detail,
        }
    return {
        "running": True,
        "login_present": login,
        "pid": pid,
        "name": _session_name(),
        "session_url": _session_url(),
        "working_dir": str(_launch_dir()),
    }


def start(name: Optional[str] = None) -> dict[str, Any]:
    """Launch the Remote Control server (idempotent, race-safe). Returns the status
    dict, or ``{ok: False, error}`` on a missing login or a launch failure."""
    with _lock():
        if _running_pid() is not None:
            return {"ok": True, "already_running": True, **status()}

        if not native_login_present():
            return {
                "ok": False,
                "running": False,
                "login_present": False,
                "error": "no_native_login",
                "detail": (
                    "Remote Control needs a native claude.ai login "
                    "(~/.claude/.credentials.json). Run the Connect Claude flow first."
                ),
            }

        label = (name or "").strip() or _default_name()
        try:
            ensure_gates_seeded()
            binary = resolve_binary("CLAUDE_CLI_PATH", "claude")

            # Clear the previous run's link/errors so status() never surfaces a
            # stale link before this session's link appears.
            _cleanup_state()

            env = _child_env()
            # NB: no `-c`/`--continue` — it errors ("No recent session found") on
            # every first Start. Each Start creates a fresh session. Args/paths pass
            # via env so the label can't break the shell.
            env["RC_BIN"] = binary
            env["RC_NAME"] = label
            env["RC_URL"] = str(URL_FILE)
            env["RC_ERR"] = str(ERR_FILE)

            proc = subprocess.Popen(
                ["bash", "-c", _LAUNCH_SCRIPT],
                cwd=str(_launch_dir()),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # own session/group → detached, killpg-able
            )
            PID_FILE.write_text(str(proc.pid))
            NAME_FILE.write_text(label)
        except Exception as exc:  # disk error, missing bash/binary, etc.
            logger.exception("Remote Control start failed")
            _cleanup_state()
            return {
                "ok": False,
                "running": False,
                "error": "start_failed",
                "detail": str(exc),
            }

        if not _wait_for_start():
            detail = _last_error() or "Remote Control exited before it became ready."
            record_auth_failure(detail)
            _cleanup_state(keep_error=True)
            return {
                "ok": False,
                "running": False,
                "login_present": native_login_present(),
                "error": "start_failed",
                "detail": detail,
            }

        logger.info("Remote Control started: pid=%s dir=%s name=%r",
                    proc.pid, _launch_dir(), label)
        return {"ok": True, "already_running": False, **status()}


def stop() -> dict[str, Any]:
    """Stop the Remote Control server. Kills the whole process group so neither the
    CLI nor the filter pipeline survives. Idempotent, race-safe."""
    with _lock():
        pid = _read_pid()
        if pid is None:
            _cleanup_state()
            return {"ok": True, "running": False}

        if _pid_alive(pid):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            # brief grace, then hard kill if still up
            for _ in range(10):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                os.waitpid(pid, 0)
            except (ChildProcessError, OSError):
                pass

        _cleanup_state()
        logger.info("Remote Control stopped: pid=%s", pid)
        return {"ok": True, "running": False}
