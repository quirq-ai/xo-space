"""
GitHub connector — `gh auth login` (CLI device-flow) acquisition.

Spawns `gh auth login --web` as a subprocess, parses the one-time device code
from its output, and waits asynchronously for the user to authorize on
github.com. Once `gh` exits successfully, the resulting token is read with
`gh auth token` and exported into token.json by `connect()`.

This sits alongside the PAT flow (github_pat.py) — the two methods share the
same storage and validation (common.py); only the *acquisition* differs.

Caveats (intentional, per Option B):
  - In-memory session state. A FastAPI worker restart drops in-progress logins.
  - Output parsing depends on `gh` CLI version 2.x stdout format.
  - At most one login session is active at a time (per process).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .common import (
    configure_git_identity,
    connection_payload,
    save_github_token,
    validate_token,
)

log = logging.getLogger(__name__)

AUTH_METHOD = "cli"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GH_BIN = "gh"
GITHUB_HOSTNAME = "github.com"
VERIFICATION_URI = "https://github.com/login/device"

# Time we'll wait for `gh` to print the device code on startup.
DEVICE_CODE_TIMEOUT_SECONDS = 15

# How long a session may sit in "pending" before we treat it as expired.
# GitHub device codes expire in 15 min — match that.
SESSION_TTL_SECONDS = 15 * 60

# Matches gh's one-time code, e.g. "7B79-D4F8".
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")


# ---------------------------------------------------------------------------
# Session state (in-memory, single-process)
# ---------------------------------------------------------------------------

@dataclass
class _Session:
    session_id: str
    process: asyncio.subprocess.Process
    user_code: str
    started_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | completed | failed | cancelled
    error: str | None = None
    token: str | None = None
    # Keeps the background reader alive for the lifetime of the subprocess.
    drain_task: asyncio.Task | None = None


_active: dict[str, _Session] = {}
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gh_available() -> bool:
    return shutil.which(GH_BIN) is not None


def _evict_stale_locked() -> None:
    """Drop sessions older than SESSION_TTL_SECONDS. Caller holds _lock."""
    now = time.time()
    stale = [sid for sid, s in _active.items() if now - s.started_at > SESSION_TTL_SECONDS]
    for sid in stale:
        s = _active.pop(sid, None)
        if s and s.process.returncode is None:
            try:
                s.process.kill()
            except ProcessLookupError:
                pass


async def _read_until_code(proc: asyncio.subprocess.Process) -> str:
    """
    Read merged stdout/stderr line-by-line until we find the device code.
    Returns the parsed code (e.g. "467D-ACBD").

    In non-TTY mode `gh` writes "First copy your one-time code: XXXX-XXXX"
    to stderr, then a URL line, then begins polling silently. We return as
    soon as we see the code — the background drain task takes over from there.

    Raises RuntimeError if no code is found before the deadline.
    """
    deadline = time.time() + DEVICE_CODE_TIMEOUT_SECONDS

    assert proc.stdout is not None  # we asked for PIPE
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise RuntimeError(
                "Timed out waiting for `gh auth login` to print a device code."
            )
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "Timed out waiting for `gh auth login` to print a device code."
            )
        if not line:
            # Process closed stdout without giving us a code.
            raise RuntimeError(
                "`gh auth login` exited before producing a device code."
            )
        text = line.decode("utf-8", errors="replace")
        m = _DEVICE_CODE_RE.search(text)
        if m:
            return m.group(1)


async def _finalize_session(proc: asyncio.subprocess.Process, sid: str) -> None:
    """Record the outcome of `gh auth login` on the session, if still pending."""
    async with _lock:
        session = _active.get(sid)
        if not session:
            return
        if session.status not in ("pending",):
            return  # already finalized (e.g. cancelled)
        if proc.returncode == 0:
            token = await _read_gh_token()
            if token:
                session.status = "completed"
                session.token = token
            else:
                session.status = "failed"
                session.error = "Login succeeded but `gh auth token` returned no token."
        else:
            session.status = "failed"
            session.error = (
                f"`gh auth login` exited with status {proc.returncode}."
            )


async def _drain_until_exit(proc: asyncio.subprocess.Process, sid: str) -> None:
    """Background task: drain stdout and update session status on exit."""
    try:
        if proc.stdout is not None:
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
            except Exception:
                pass

        await proc.wait()
    finally:
        # No `return` here on purpose: a return inside `finally` discards the
        # in-flight exception — including the CancelledError that
        # cancel_session() delivers — so the task would report success after
        # being cancelled. Python 3.14 warns about exactly this (PEP 765).
        await _finalize_session(proc, sid)


async def _read_gh_token() -> str | None:
    """Fetch the active github.com token via `gh auth token`."""
    try:
        proc = await asyncio.create_subprocess_exec(
            GH_BIN, "auth", "token", "--hostname", GITHUB_HOSTNAME,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
        log.warning("Failed to read gh token: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    token = stdout.decode("utf-8", errors="replace").strip()
    return token or None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def start_login() -> dict[str, Any]:
    """
    Spawn `gh auth login --web`, parse the device code, and return the
    user-facing details. The subprocess continues running in the background
    until the user authorizes on github.com (or the code expires).
    """
    if not _gh_available():
        raise RuntimeError(
            "GitHub CLI (`gh`) is not installed on the server. "
            "Install it from https://cli.github.com/ or use the PAT method instead."
        )

    # Hold the lock for the whole start so two concurrent /cli/start calls
    # can't race past the "is something pending?" check and both spawn a
    # `gh auth login` (which would clobber each other's local state).
    async with _lock:
        _evict_stale_locked()
        for s in _active.values():
            if s.status == "pending":
                raise RuntimeError(
                    "A GitHub CLI login is already in progress. "
                    "Cancel it first or wait for it to complete."
                )

        env = os.environ.copy()
        # Prevent gh from trying to launch a browser on the server.
        env["BROWSER"] = "true"

        # Clear any prior `gh` session for github.com — `gh auth login` refuses
        # to start a fresh device flow when an account is already logged in.
        # Errors here are non-fatal (e.g. "not logged in" exits non-zero).
        try:
            logout = await asyncio.create_subprocess_exec(
                GH_BIN, "auth", "logout", "--hostname", GITHUB_HOSTNAME,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(logout.wait(), timeout=5)
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            pass

        # `--insecure-storage` writes the token to a plain file under
        # ~/.config/gh — fine here because we immediately export it into
        # token.json and never depend on gh's local store after that.
        proc = await asyncio.create_subprocess_exec(
            GH_BIN, "auth", "login",
            "--web",
            "--hostname", GITHUB_HOSTNAME,
            "--git-protocol", "https",
            "--skip-ssh-key",
            "--insecure-storage",
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            user_code = await _read_until_code(proc)
        except Exception:
            # Something went wrong before we got a code — clean up.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise

        sid = uuid.uuid4().hex
        session = _Session(session_id=sid, process=proc, user_code=user_code)
        _active[sid] = session
        session.drain_task = asyncio.create_task(_drain_until_exit(proc, sid))

    log.info("gh auth login session %s started (code=%s)", sid, user_code)
    return {
        "session_id": sid,
        "user_code": user_code,
        "verification_uri": VERIFICATION_URI,
        "expires_in": SESSION_TTL_SECONDS,
    }


async def poll_login(session_id: str) -> dict[str, Any]:
    """
    Check the status of an in-progress login. Returns one of:
      - {"status": "pending",   "user_code": ..., "verification_uri": ...}
      - {"status": "completed", "token": ...}              (consume once)
      - {"status": "failed",    "error": ...}
      - {"status": "not_found"}                            (unknown session_id)
    """
    async with _lock:
        _evict_stale_locked()
        session = _active.get(session_id)
        if not session:
            return {"status": "not_found"}

        if session.status == "pending":
            return {
                "status": "pending",
                "user_code": session.user_code,
                "verification_uri": VERIFICATION_URI,
            }

        # Terminal state — pop so subsequent polls return not_found.
        _active.pop(session_id, None)

        if session.status == "completed" and session.token:
            return {"status": "completed", "token": session.token}

        return {
            "status": session.status,
            "error": session.error or "Login failed.",
        }


async def connect(session_id: str) -> dict[str, Any]:
    """
    Poll a login session and, once `gh` hands us a token, validate + store it.

    Mirrors ``github_pat.connect``, so both flows converge on the same stored
    entry and the same response body. Returns:
        {"ok": True,  "payload": <connection body>}      login finished
        {"ok": False, "status": "pending", ...}          user hasn't authorized yet
        {"ok": False, "status": "not_found"}             unknown/expired session
        {"ok": False, "status": "failed", "error": ...}  login or validation failed
    """
    result = await poll_login(session_id)
    status = result.get("status")

    if status != "completed":
        return {"ok": False, **result}

    token = result["token"]
    validation = await validate_token(token)
    if not validation.get("valid"):
        return {
            "ok": False,
            "status": "failed",
            "error": validation.get(
                "error",
                "GitHub CLI login completed but the token failed validation.",
            ),
        }

    save_github_token(token, auth_method=AUTH_METHOD)
    # This flow leaves a live `gh` session behind, so git can borrow it for
    # HTTPS auth as well as take its identity from it.
    await configure_git_identity(validation, setup_credential_helper=True)
    log.info("GitHub connected as @%s (via gh CLI)", validation.get("username"))
    return {"ok": True, "payload": connection_payload(validation, AUTH_METHOD)}


async def cancel_login(session_id: str) -> dict[str, Any]:
    """Kill an in-progress login and forget the session."""
    async with _lock:
        session = _active.pop(session_id, None)
    if not session:
        return {"status": "not_found"}

    if session.process.returncode is None:
        try:
            session.process.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(session.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            log.warning("gh subprocess for session %s did not exit promptly", session_id)

    return {"status": "cancelled"}
