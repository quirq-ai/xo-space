"""
Shared rclone connector — generic CLI plumbing + an OAuth-flow engine driven
by a per-provider descriptor.

Every rclone-backed cloud connector (Google Drive, OneDrive, …) runs the same
``rclone authorize`` subprocess dance and the same session lifecycle; only a
handful of provider facts differ (the backend name, the auth-URL pattern, how
the captured token becomes an ``rclone.conf`` section, and how a configured
remote is summarised). Those facts live in :class:`RcloneProvider`; the
mechanics live in :class:`RcloneConnector`.

Layering:
  * Low-level CLI helpers (``_rclone_cli``, ``_rc_post``, streaming upload) and
    the rclone OAuth helpers (``_PipeReader``, ``_resolve_oauth_url``,
    ``_deliver_code_to_rclone``) are provider-agnostic and live here so no
    connector has to reach into another's internals.
  * :class:`RcloneConnector` owns the session store, registers with the
    cross-connector OAuth port lock, and runs ``_run_oauth_flow`` against its
    provider descriptor.

All operations invoke ``rclone`` as a subprocess (CLI mode — no ``rclone rcd``
daemon, no port held open except rclone's transient :53682 OAuth callback).
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Optional

import httpx

from .oauth_lock import (
    cancel_all_active_oauth,
    has_active_oauth,
    register_sessions,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# rclone config file — the remotes of every rclone-backed connector (gdrive,
# onedrive, …). Kept in the user's config directory so connected accounts
# survive a redeploy and a checkout carries no secrets. The path is rclone's
# own default (`rclone config file`), so a remote this API configures is also
# visible to `rclone` typed in a terminal, with no --config to remember.
# Pre-move location: `services/rclone.conf`, four dirnames up from this file.
# Any file left there is moved here at import (_migrate_legacy_config).
_LEGACY_CONFIG_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
_LEGACY_RCLONE_CONFIG = os.path.join(_LEGACY_CONFIG_DIR, "rclone.conf")

RCLONE_CONFIG_PATH = os.path.expanduser(
    os.getenv("RCLONE_CONFIG") or str(Path.home() / ".config" / "rclone" / "rclone.conf")
)


def _migrate_legacy_config() -> None:
    """Move a pre-existing services/rclone.conf to RCLONE_CONFIG_PATH, once.

    Runs at import so the path is settled before any rclone subprocess sees
    ``--config``. No-op unless a legacy file exists and the new one doesn't;
    a failure is logged, leaving the legacy file untouched for a manual move.
    """
    if RCLONE_CONFIG_PATH == _LEGACY_RCLONE_CONFIG:
        return
    if os.path.exists(RCLONE_CONFIG_PATH) or not os.path.exists(_LEGACY_RCLONE_CONFIG):
        return
    try:
        os.makedirs(os.path.dirname(RCLONE_CONFIG_PATH), exist_ok=True)
        shutil.move(_LEGACY_RCLONE_CONFIG, RCLONE_CONFIG_PATH)
        os.chmod(RCLONE_CONFIG_PATH, 0o600)  # holds OAuth tokens
        log.info("Moved rclone config %s -> %s", _LEGACY_RCLONE_CONFIG, RCLONE_CONFIG_PATH)
    except OSError as exc:
        log.warning(
            "Could not move %s to %s: %s — connected remotes may need re-authorizing",
            _LEGACY_RCLONE_CONFIG, RCLONE_CONFIG_PATH, exc,
        )


def _ensure_config_dir() -> None:
    """Create the config directory so rclone (and our own append) can write."""
    parent = os.path.dirname(RCLONE_CONFIG_PATH)
    if not parent:
        return
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        log.warning("Could not create %s: %s", parent, exc)


_migrate_legacy_config()
_ensure_config_dir()

# rclone's OAuth callback port — hardcoded by the providers' OAuth client
# registration (embedded in rclone's bundled credentials).
RCLONE_OAUTH_PORT = int(os.getenv("RCLONE_OAUTH_PORT", "53682"))

SESSION_TTL   = 600   # 10 min
OAUTH_TIMEOUT = 300   # 5 min

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


# ---------------------------------------------------------------------------
# rclone CLI helpers (provider-agnostic)
# ---------------------------------------------------------------------------

async def _rclone_cli(*args: str, timeout: int = 30) -> str:
    """Run an ``rclone`` CLI command and return stdout. Raises RuntimeError on
    non-zero exit. Always passes ``--config`` so commands target our project's
    rclone.conf."""
    full_args = ("rclone",) + args + ("--config", RCLONE_CONFIG_PATH)
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("rclone binary not found in PATH") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"rclone {args[0] if args else ''} timed out after {timeout}s")

    if proc.returncode != 0:
        err_text = (stderr or stdout).decode(errors="replace").strip()
        raise RuntimeError(f"rclone error: {err_text or f'exit code {proc.returncode}'}")
    return stdout.decode(errors="replace")


async def _rclone_cli_stdin_stream(
    *args: str,
    chunk_iter: AsyncIterator[bytes],
) -> None:
    """Spawn ``rclone`` and stream ``chunk_iter`` to its stdin. Raises
    RuntimeError on non-zero exit (with stderr tail). Always passes ``--config``.

    No subprocess timeout — the natural backstop is the caller's HTTP request.
    On any error or early return, kills the subprocess in a finally block to
    avoid zombie rclone processes."""
    full_args = ("rclone",) + args + ("--config", RCLONE_CONFIG_PATH)
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("rclone binary not found in PATH") from exc

    assert proc.stdin is not None and proc.stderr is not None

    # Bounded stderr ring (last 64 KiB) for diagnostics on failure.
    stderr_buf = bytearray()
    _STDERR_MAX = 64 * 1024

    async def drain_stderr() -> None:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            stderr_buf.extend(chunk)
            if len(stderr_buf) > _STDERR_MAX:
                del stderr_buf[: len(stderr_buf) - _STDERR_MAX]

    client_disconnected = False

    async def feed_stdin() -> None:
        nonlocal client_disconnected
        try:
            async for chunk in chunk_iter:
                if not chunk:
                    continue
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # rclone closed stdin (e.g. exited early on validation failure).
            # Swallow — the non-zero return code surfaces the real error.
            pass
        except Exception as exc:
            # Starlette raises ClientDisconnect when the browser/proxy drops
            # the upload mid-stream. Re-raise as a clean RuntimeError so the
            # caller's exception handler maps it to a useful HTTP error.
            if exc.__class__.__name__ == "ClientDisconnect":
                client_disconnected = True
                return
            raise
        finally:
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    stderr_task = asyncio.create_task(drain_stderr())
    try:
        await feed_stdin()
        rc = await proc.wait()
        await stderr_task
        if client_disconnected:
            raise RuntimeError(
                "Upload connection dropped before all bytes arrived. "
                "If you're behind the Next.js dev proxy, configure "
                "NEXT_PUBLIC_XO_COWORK_API_URL to bypass it."
            )
        if rc != 0:
            tail = bytes(stderr_buf).decode(errors="replace").strip()
            raise RuntimeError(tail or f"rclone exit {rc}")
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
        if not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass


async def _rc_post(endpoint: str, body: dict | None = None, timeout: int = 15) -> dict:
    """Compatibility shim: maps the legacy rclone rc HTTP endpoints used by this
    project to local ``rclone`` CLI invocations, so we don't need a daemon
    listening on a port. Return shapes match the original rc JSON responses."""
    body = body or {}
    ep = endpoint.lstrip("/")

    if ep == "rc/noop":
        await _rclone_cli("version", timeout=timeout)
        return {}

    if ep == "config/listremotes":
        out = await _rclone_cli("listremotes", timeout=timeout)
        names = [line.rstrip(":").strip() for line in out.splitlines() if line.strip()]
        return {"remotes": names}

    if ep == "config/get":
        name = body.get("name", "")
        out = await _rclone_cli("config", "dump", timeout=timeout)
        try:
            dump = json.loads(out) if out.strip() else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Could not parse rclone config dump: {exc}") from exc
        return dump.get(name, {})

    if ep == "config/delete":
        name = body.get("name", "")
        if not name:
            raise RuntimeError("config/delete requires a 'name'")
        await _rclone_cli("config", "delete", name, timeout=timeout)
        return {}

    raise RuntimeError(f"Unsupported rclone CLI shim endpoint: {endpoint}")


# ---------------------------------------------------------------------------
# Availability checks (CLI mode — no daemon to keep alive)
# ---------------------------------------------------------------------------

async def ensure_rclone_running() -> None:
    """No-op in CLI mode. Logs whether the binary is reachable so startup
    surfaces missing-binary problems early instead of at first request."""
    try:
        await _rclone_cli("version", timeout=5)
        log.info("rclone CLI available (no daemon needed)")
    except Exception as exc:
        log.warning("rclone not available: %s", exc)


async def rclone_available() -> bool:
    try:
        await _rclone_cli("version", timeout=5)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# OAuth helpers (provider-agnostic)
# ---------------------------------------------------------------------------

class _PipeReader:
    """Drains a subprocess pipe in a background thread.

    This prevents the classic subprocess deadlock where one pipe's buffer
    fills up because we stopped reading it, blocking the child process from
    writing to the OTHER pipe we're trying to read."""

    def __init__(self, pipe, name: str = "pipe"):
        self.lines: list[str] = []
        self.name = name
        self._pipe = pipe
        self._thread = threading.Thread(target=self._drain, daemon=True, name=f"rclone-{name}")
        self._thread.start()

    def _drain(self):
        try:
            for raw_line in self._pipe:
                line = raw_line.decode(errors="replace").strip()
                if line:
                    self.lines.append(line)
        except Exception:
            pass

    def join(self, timeout: float | None = None):
        self._thread.join(timeout=timeout)


async def _resolve_oauth_url(local_auth_url: str) -> tuple[str, str]:
    """GET rclone's local ``/auth?state=...`` endpoint (no redirect-following)
    and return (provider_url, state). rclone responds with HTTP 307 + Location
    set to the real provider OAuth URL (Google, Microsoft, …)."""
    parsed = urllib.parse.urlparse(local_auth_url)
    state = urllib.parse.parse_qs(parsed.query).get("state", [""])[0]
    if not state:
        raise RuntimeError(f"Could not parse state from {local_auth_url!r}")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        resp = await client.get(local_auth_url)
        if resp.status_code not in (301, 302, 303, 307, 308):
            raise RuntimeError(
                f"rclone /auth returned HTTP {resp.status_code} (expected redirect)"
            )
        location = resp.headers.get("location")
        if not location:
            raise RuntimeError("rclone /auth response had no Location header")
    return location, state


async def _deliver_code_to_rclone(state: str, code: str) -> None:
    """Deliver the OAuth code to rclone's local callback server."""
    callback_url = (
        f"http://127.0.0.1:{RCLONE_OAUTH_PORT}/"
        f"?state={urllib.parse.quote(state, safe='')}"
        f"&code={urllib.parse.quote(code, safe='')}"
    )
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        await client.get(callback_url)


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------

SessionStatus = Literal["pending", "awaiting_oauth", "completed", "failed", "cancelled"]


@dataclass
class RcloneSession:
    session_id: str
    remote_name: str
    status: SessionStatus = "pending"
    auth_url: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    oauth_started_at: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    # Set by the route handler when the user pastes the redirect URL.
    verification_input: str | None = None
    # Always True — manual paste is the only supported path. The user's browser
    # cannot reach the workspace's :53682 unless an IDE port-forward is active,
    # so we don't rely on an automatic browser-side callback.
    needs_manual_code: bool = True
    # rclone's OAuth state token, captured from the local /auth URL and replayed
    # when delivering the code to rclone's local callback.
    oauth_state: str | None = None


# ---------------------------------------------------------------------------
# Provider descriptor — the per-backend deltas
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RcloneProvider:
    """Everything that differs between rclone-backed connectors."""

    backend: str                          # rclone authorize <backend> (e.g. "drive")
    authorize_args: tuple[str, ...]       # extra args after the backend name
    label: str                            # log prefix, e.g. "GDrive" / "OneDrive"
    provider_name: str                    # error text, e.g. "Google" / "Microsoft"
    auth_url_re: "re.Pattern[str]"        # matches the provider auth URL in a log line
    # (name, token_json) -> the rclone.conf INI section to append. Async because
    # some providers (onedrive) make an API call here. Raise RuntimeError with a
    # user-facing message to fail the flow.
    build_config_section: Callable[[str, str], Awaitable[str]]
    # (name, cfg_dict) -> a listing row for /remotes, or None to skip (wrong type).
    remote_summary: Callable[[str, dict], Optional[dict]]


# ---------------------------------------------------------------------------
# Connector — session lifecycle + OAuth flow engine
# ---------------------------------------------------------------------------

class RcloneConnector:
    """One instance per provider. Owns its session store and runs the shared
    OAuth flow against the provider descriptor."""

    def __init__(self, provider: RcloneProvider):
        self.provider = provider
        self._sessions: dict[str, RcloneSession] = {}
        # Register with the cross-connector OAuth lock — all rclone connectors
        # share port 53682, so only ONE OAuth flow can be active at a time.
        register_sessions(
            lambda: self._sessions.values(),
            lambda sid: self.cancel_session(sid),
        )

    # ---- session accessors ------------------------------------------------

    def get_session(self, session_id: str) -> RcloneSession | None:
        return self._sessions.get(session_id)

    def _expire_sessions(self) -> None:
        now = time.time()
        for sid in [k for k, v in self._sessions.items() if now - v.created_at > SESSION_TTL]:
            s = self._sessions.pop(sid)
            if s.task and not s.task.done():
                s.task.cancel()

    # ---- remote listing / validation -------------------------------------

    async def list_remotes(self) -> list[dict]:
        try:
            data = await _rc_post("/config/listremotes")
            names: list[str] = data.get("remotes") or []
        except Exception as exc:
            raise RuntimeError(f"Could not reach rclone: {exc}") from exc

        remotes = []
        for name in names:
            try:
                cfg = await _rc_post("/config/get", {"name": name})
                row = self.provider.remote_summary(name, cfg)
                if row is not None:
                    remotes.append(row)
            except Exception:
                pass
        return remotes

    async def validate_remote_name(self, name: str) -> str | None:
        if not _NAME_RE.match(name):
            return "Name must be 1-32 chars: lowercase letters, digits, _ or -"
        try:
            data = await _rc_post("/config/listremotes")
            if name in (data.get("remotes") or []):
                return "A remote with this name already exists."
        except Exception:
            pass
        return None

    # ---- public lifecycle -------------------------------------------------

    async def create_remote_session(self, name: str, force: bool = False) -> RcloneSession:
        self._expire_sessions()
        if force:
            await cancel_all_active_oauth()
        if has_active_oauth():
            raise RuntimeError("Another connection is being set up. Please finish or cancel it first.")

        session_id = str(uuid.uuid4())
        session = RcloneSession(session_id=session_id, remote_name=name)
        self._sessions[session_id] = session
        session.task = asyncio.create_task(self._run_oauth_flow(session))
        return session

    async def cancel_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return
        session.status = "cancelled"
        if session.task and not session.task.done():
            session.task.cancel()
        try:
            await _rc_post("/config/delete", {"name": session.remote_name})
        except Exception:
            pass
        self._sessions.pop(session_id, None)

    async def delete_remote(self, name: str) -> None:
        await _rc_post("/config/delete", {"name": name})

    # ---- OAuth flow -------------------------------------------------------

    def _extract_auth_url(self, line: str) -> str | None:
        m = self.provider.auth_url_re.search(line)
        return m.group(0) if m else None

    async def _run_oauth_flow(self, session: RcloneSession) -> None:
        """Complete OAuth via ``rclone authorize`` subprocess + manual paste.

        Both stdout and stderr are drained concurrently via background threads
        to prevent pipe buffer deadlocks.

        Flow:
          1. Spawn: rclone authorize --auth-no-open-browser <backend> [args...]
          2. Poll stderr for the local URL → resolve provider URL via 307 redirect
          3. User signs in; pastes the redirect URL into the UI
          4. Bridge delivers the code locally to rclone's callback server
          5. Poll stdout for token JSON
          6. Provider turns the token into a config section; append to rclone.conf
          7. Verify the remote shows up complete
        """
        p = self.provider
        name = session.remote_name
        proc: subprocess.Popen | None = None

        try:
            # ── 1. Spawn rclone authorize ────────────────────────────────
            log.info("%s %s: spawning rclone authorize", p.label, session.session_id)
            proc = subprocess.Popen(
                ["rclone", "authorize", "--auth-no-open-browser", p.backend,
                 *p.authorize_args,
                 f"--config={RCLONE_CONFIG_PATH}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            stderr_reader = _PipeReader(proc.stderr, "stderr")
            stdout_reader = _PipeReader(proc.stdout, "stdout")

            # ── 2. Wait for the local auth URL on stderr ─────────────────
            local_auth_url: str | None = None
            url_deadline = time.time() + 15
            while time.time() < url_deadline:
                if session.status == "cancelled":
                    proc.kill()
                    return
                for line in stderr_reader.lines:
                    url = self._extract_auth_url(line)
                    if url:
                        local_auth_url = url
                        break
                if local_auth_url:
                    break
                await asyncio.sleep(0.3)

            if not local_auth_url:
                session.status = "failed"
                session.error = (
                    "rclone authorize did not produce an auth URL.\n"
                    + "\n".join(stderr_reader.lines[-5:])
                )
                proc.kill()
                return

            # Resolve the actual provider URL via the local /auth → 307 Location.
            try:
                provider_url, state = await _resolve_oauth_url(local_auth_url)
            except Exception as exc:
                session.status = "failed"
                session.error = f"Could not resolve {p.provider_name} auth URL: {exc}"
                proc.kill()
                return

            session.auth_url = provider_url
            session.oauth_state = state
            session.oauth_started_at = time.time()
            session.status = "awaiting_oauth"
            log.info(
                "%s %s: %s auth URL ready (state=%s…)",
                p.label, session.session_id, p.provider_name, state[:8],
            )

            # ── 3. Wait for paste → deliver code locally → rclone exits ──
            log.info("%s %s: waiting for user to paste redirect URL...", p.label, session.session_id)
            complete_deadline = time.time() + OAUTH_TIMEOUT
            delivered = False
            while time.time() < complete_deadline:
                if session.status == "cancelled":
                    proc.kill()
                    return
                if proc.poll() is not None:
                    break  # rclone exited (success or error)
                if not delivered and session.verification_input:
                    code = session.verification_input
                    try:
                        await _deliver_code_to_rclone(state, code)
                        delivered = True
                        log.info("%s %s: delivered code to rclone callback", p.label, session.session_id)
                    except Exception as exc:
                        log.warning("%s %s: delivery failed: %s", p.label, session.session_id, exc)
                        session.verification_input = None
                await asyncio.sleep(1)
            else:
                session.status = "failed"
                session.error = (
                    "Timed out waiting for paste. Click Cancel and try again."
                    if not delivered
                    else "Timed out after delivering the code to rclone."
                )
                proc.kill()
                return

            # Give reader threads a moment to flush.
            await asyncio.sleep(0.5)
            exit_code = proc.returncode
            log.info("%s %s: rclone authorize exited with code %s", p.label, session.session_id, exit_code)

            if exit_code != 0:
                session.status = "failed"
                session.error = (
                    f"rclone authorize failed (exit code {exit_code}).\n"
                    + "\n".join(stderr_reader.lines[-5:])
                )
                return

            # ── 4. Extract token JSON from captured stdout ───────────────
            token_json: str | None = None
            for line in stdout_reader.lines:
                if "access_token" in line:
                    stripped = line.strip()
                    if stripped.startswith("{"):
                        token_json = stripped
                    break

            if not token_json:
                session.status = "failed"
                session.error = (
                    "Auth succeeded but no token was found in rclone output.\n"
                    "stdout lines: " + " | ".join(stdout_reader.lines[-5:])
                )
                return

            log.info("%s %s: captured token (%d chars)", p.label, session.session_id, len(token_json))

            # ── 5. Provider builds the rclone.conf section (may call an API) ─
            try:
                config_section = await p.build_config_section(name, token_json)
            except Exception as exc:
                session.status = "failed"
                session.error = str(exc)
                return

            # ── 6. Write the remote directly to rclone.conf ──────────────
            #    We append the INI section directly instead of invoking
            #    `rclone config create`, which would try to validate/refresh
            #    the token and start another auth webserver on :53682 — a port
            #    conflict with our own listener. Subsequent CLI calls re-read
            #    rclone.conf, so the new remote is visible immediately.
            try:
                with open(RCLONE_CONFIG_PATH, "a", encoding="utf-8") as f:
                    f.write(config_section)
                log.info("%s %s: wrote remote '%s' to %s", p.label, session.session_id, name, RCLONE_CONFIG_PATH)
            except Exception as exc:
                session.status = "failed"
                session.error = f"Could not write rclone.conf: {exc}"
                return

            # ── 7. Verify the remote is configured ───────────────────────
            for _ in range(10):
                if session.status == "cancelled":
                    return
                await asyncio.sleep(1)
                try:
                    remotes = await self.list_remotes()
                    found = next((r for r in remotes if r["name"] == name), None)
                    if found and found.get("complete"):
                        session.status = "completed"
                        log.info("%s %s: remote '%s' ready ✓", p.label, session.session_id, name)
                        return
                except Exception:
                    pass

            session.status = "failed"
            session.error = "Remote was created but verification failed. Try refreshing."

        except asyncio.CancelledError:
            session.status = "cancelled"
            if proc and proc.poll() is None:
                proc.kill()
        except Exception as exc:
            log.exception("%s OAuth error in session %s", p.label, session.session_id)
            session.status = "failed"
            session.error = str(exc)
            if proc and proc.poll() is None:
                proc.kill()
            try:
                await _rc_post("/config/delete", {"name": name})
            except Exception:
                pass
