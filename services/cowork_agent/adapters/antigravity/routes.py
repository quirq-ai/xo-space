"""
Connect Antigravity — agent-owned OAuth login flow (the ``routes`` capability).

Mounted **only when ``AGENT_NAME=antigravity``** (via ``_active_agent_routes`` in
``server.py``), so the agent literal stays inside the
adapter tree and the modularity invariant holds — this is why the flow lives here
rather than in ``routers/auth/`` like ``claude_setup_token.py``.

Mechanism (mirrors Connect Claude, adapted to agy):
  * agy has **no ``auth login`` subcommand**; a logged-out ``agy -p`` prints
    ``Authentication required. Please visit the URL to log in:`` followed by a
    Google OAuth URL, then waits and reads a pasted authorization code from stdin
    (verified live). So the login *trigger* is a throwaway ``agy -p`` under a PTY.
  * The authoritative "connected" check is the **token file** (agy has no auth
    CLI): ``adapters/antigravity/auth.has_usable_login()``. A background poller
    flips the flow to success the instant the token lands — so we never wait for
    (or pay for) the throwaway prompt to run; we kill agy as soon as auth writes
    the token.

Flow:
  POST /connect/antigravity (SSE)
    1. If already logged in → emit ``done`` immediately (idempotent).
    2. Spawn ``agy -p … --dangerously-skip-permissions`` under a PTY.
    3. Extract the OAuth URL from its output → emit ``auth_url``.
  POST /connect/antigravity/callback
    4. Write the pasted code (+CR) to the PTY master.
    5. When ``has_usable_login()`` flips true (or agy exits 0) → emit ``done``,
       kill agy, clear session. On agy error output / non-zero exit → ``error``.

Event shapes match Connect Claude (``session`` / ``auth_url`` / ``stdout`` /
``done`` / ``error``) so the frontend Connect component is reusable with a
different endpoint. On Unix a PTY is used; set ANTIGRAVITY_CONNECT_USE_PTY=0 to
force pipe mode (login prompts generally need the PTY).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from typing import AsyncGenerator, Optional
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.cowork_agent.adapters.antigravity.auth import has_usable_login

try:
    import pty as _pty
    import fcntl
    import struct
    import termios

    _HAS_PTY = hasattr(_pty, "openpty") and sys.platform != "win32"
except ImportError:
    _HAS_PTY = False


router = APIRouter(tags=["antigravity-connect"])


# ── ANSI stripping + URL extraction ───────────────────────────────────────────

_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_2CHAR_RE = re.compile(r"\x1b[@-Z\\-_]")
_ANSI_NF_RE = re.compile(r"\x1b[\x20-\x2f]+[\x30-\x7e]")


def _strip_ansi(line: str) -> str:
    line = _ANSI_OSC_RE.sub("", line)
    line = _ANSI_CSI_RE.sub("", line)
    line = _ANSI_NF_RE.sub("", line)
    line = _ANSI_2CHAR_RE.sub("", line)
    return line.replace("\x07", "")


def _err_event(text: str) -> str:
    """Build an SSE error frame.

    Carries the reason under BOTH keys on purpose: the deployed xo-swarm dialog
    reads `data.message` (setup-antigravity-dialog.tsx:129) and renders a generic
    "Stream error" without it, while `error` is kept for any other consumer.
    Emitting both from one place keeps them from drifting apart.
    """
    return f"data: {json.dumps({'type': 'error', 'error': text, 'message': text})}\n\n"


# agy prints the OAuth URL on its own (indented) line after
# "Please visit the URL to log in:". Capture the Google OAuth authorize URL
# (host-flexible: any https URL bearing the oauth2 authorize path), with a
# redirect_uri-bearing fallback.
_AUTH_URL_RE = re.compile(r"https://[^\s\"'<>\x00-\x1f]*/o/oauth2/[^\s\"'<>\x00-\x1f]+")
_AUTH_URL_FALLBACK_RE = re.compile(r"https://[^\s\"'<>\x00-\x1f]+redirect_uri=[^\s\"'<>\x00-\x1f]+")


def _extract_auth_url(text: str) -> Optional[str]:
    m = _AUTH_URL_RE.search(text) or _AUTH_URL_FALLBACK_RE.search(text)
    if not m:
        return None
    raw = m.group(0)
    # Drop a concatenated redraw if the URL appears twice back-to-back.
    second = raw.find("https://", len("https://"))
    if second != -1:
        raw = raw[:second]
    return raw


def _normalize_code(raw_value: str) -> str:
    """Normalize a pasted value to the authorization code agy expects.

    Accepts the raw code, a full redirect URL, or a ``code=…&state=…`` string.
    agy already holds the PKCE verifier/state for the session, so we hand it the
    bare ``code`` when we can extract one; otherwise pass the value through."""
    value = (raw_value or "").strip()
    if not value:
        return value
    if value.startswith("http://") or value.startswith("https://"):
        params = parse_qs(urlparse(value).query)
        code = (params.get("code") or [None])[0]
        return code or value
    if "code=" in value:
        params = parse_qs(value)
        code = (params.get("code") or [None])[0]
        if code:
            return code
    return value


def _resolve_agy_path() -> str:
    path = (os.getenv("AGY_CLI_PATH") or "agy").strip()
    if not os.path.isabs(path):
        return shutil.which(path) or path
    return path


CONNECT_TIMEOUT_SECONDS = int(os.getenv("ANTIGRAVITY_CONNECT_TIMEOUT", "180"))
# Terminal event for a stream a newer connect took over. Mirrors the reference's
# wording (claude_setup_token.py:713): the SESSION is still alive and pastable —
# only this connection ended.
_SUPERSEDED_MESSAGE = (
    "This connection was superseded by a newer one; the login session is still active."
)
SSE_HEARTBEAT_SECONDS = int(os.getenv("ANTIGRAVITY_CONNECT_HEARTBEAT", "15"))
_TRIGGER_MODEL = "Gemini 3.5 Flash (Low)"


def _pty_wanted() -> bool:
    flag = os.getenv("ANTIGRAVITY_CONNECT_USE_PTY", "1").strip().lower()
    return flag not in ("0", "false", "no", "off") and _HAS_PTY


def _set_pty_winsize(slave_fd: int, rows: int = 24, cols: int = 1000) -> None:
    """Wide width so the long OAuth URL never wraps (wrapping corrupts extraction)."""
    if not _HAS_PTY:
        return
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _disable_pty_echo(slave_fd: int) -> None:
    """Suppress terminal echo so the pasted code never lands in logs/SSE."""
    if not _HAS_PTY:
        return
    try:
        attrs = termios.tcgetattr(slave_fd)
        attrs[3] = attrs[3] & ~termios.ECHO
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except OSError:
        pass


# ── Single in-flight session state ────────────────────────────────────────────

_lock = asyncio.Lock()
_process: Optional[asyncio.subprocess.Process] = None
_pty_master: Optional[int] = None
_session_id: Optional[str] = None
_ready: Optional[asyncio.Event] = None
_queue: Optional[asyncio.Queue] = None
_auth_url: Optional[str] = None
_started_at: Optional[float] = None
_last_completed_session_id: Optional[str] = None
_last_completed_ok: bool = False
# True once the user has pasted a code this session — suppresses the auth-timer
# respawn so we never kill an agy that is mid token-exchange.
_code_submitted: bool = False
# Ownership token for the in-flight session, ALWAYS compared by identity (`is`),
# never equality: each generate() mints its own `object()`. Invariant:
# `_owner is not None` ⟺ some generator currently owns the session. It is what
# makes attach safe — without it, a superseded generator would clear globals or
# kill a process that a newer stream has taken over. See _spawn_login/clear_session.
_owner: Optional[object] = None
# Anchors the overall budget to the ORIGINAL session start. Deliberately distinct
# from `_started_at`, which _spawn_login re-sets on EVERY respawn — anchoring the
# deadline to that would re-extend the budget on each reattach instead of bounding
# it, i.e. the exact opposite of the intent.
_session_started_at: Optional[float] = None


def _deliver(session_id: str, item: tuple) -> None:
    if _session_id == session_id and _queue is not None:
        _queue.put_nowait(item)


def _close_pty_master() -> None:
    global _pty_master
    if _pty_master is not None:
        fd = _pty_master
        _pty_master = None
        try:
            os.close(fd)
        except OSError:
            pass


async def _write_code_to_pty(text_bytes: bytes) -> None:
    if _pty_master is None:
        raise RuntimeError("No PTY channel for the login process")
    await asyncio.to_thread(os.write, _pty_master, text_bytes + b"\r")


# ── Callback ──────────────────────────────────────────────────────────────────

class AntigravityConnectCallbackBody(BaseModel):
    code: str          # the authorization code pasted from the browser
    session_id: str    # must match the SSE stream's session_id


@router.post("/connect/antigravity/callback")
async def antigravity_connect_callback(body: AntigravityConnectCallbackBody):
    """Send the pasted authorization code to the running ``agy`` login process."""
    global _process, _session_id, _ready, _code_submitted

    if _process is None or _session_id is None:
        if body.session_id == _last_completed_session_id and _last_completed_ok:
            return {"ok": True, "message": "Login already completed", "session_id": body.session_id}
        raise HTTPException(
            status_code=409,
            detail="No antigravity login session active. Start one with POST /connect/antigravity first.",
        )
    if body.session_id != _session_id:
        raise HTTPException(
            status_code=409,
            detail=f"Session mismatch (active '{_session_id}', got '{body.session_id}'). Start a new session.",
        )

    ready_event = _ready
    if ready_event is not None and not ready_event.is_set():
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Login process did not become ready in time.")

    async with _lock:
        if _process is None or _pty_master is None:
            raise HTTPException(status_code=409, detail="Login session is no longer active.")
        if body.session_id != _session_id:
            raise HTTPException(status_code=409, detail="Session changed while waiting. Start a new session.")
        if _process.returncode is not None:
            raise HTTPException(status_code=409, detail="Login process already finished. Start a new session.")
        try:
            normalized = _normalize_code(body.code)
            await _write_code_to_pty(normalized.encode("utf-8"))
            _code_submitted = True  # stop the auth-timer respawn — a token exchange is now in flight
            return {"ok": True, "message": "Code sent", "session_id": _session_id}
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise HTTPException(status_code=410, detail=f"Login process input closed: {e}")


# ── SSE login stream ──────────────────────────────────────────────────────────

@router.post("/connect/antigravity")
async def antigravity_connect():
    """Drive an ``agy`` login and stream its progress via SSE.

    agy prints an OAuth URL; the frontend shows it, the user authorizes and pastes
    the code back to POST /connect/antigravity/callback. On success agy writes its
    self-refreshing token file; we persist nothing and detect success from it."""
    global _process, _pty_master, _session_id, _ready, _queue, _auth_url, _started_at

    agy_path = _resolve_agy_path()

    async def generate() -> AsyncGenerator[str, None]:
        global _process, _pty_master, _session_id, _ready, _queue, _auth_url, _started_at
        global _last_completed_session_id, _last_completed_ok, _code_submitted
        global _owner, _session_started_at

        session_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        ready_event = asyncio.Event()
        process: Optional[asyncio.subprocess.Process] = None
        auth_url_sent = False
        # This stream's identity for every ownership check below.
        owner = object()
        adopted = False
        adopted_url: Optional[str] = None

        # Idempotent short-circuit: already logged in → nothing to do.
        if has_usable_login():
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'returncode': 0})}\n\n"
            return

        def clear_session(proc) -> None:
            async def _clear():
                global _process, _pty_master, _session_id, _ready, _queue, _auth_url, _started_at
                global _owner, _session_started_at
                async with _lock:
                    # Owner-guarded as well as proc-guarded: once a newer stream has
                    # adopted this session, THIS generator must never tear it down.
                    if _owner is owner and _process is proc:
                        _process = None
                        _session_id = None
                        _ready = None
                        _queue = None
                        _auth_url = None
                        _started_at = None
                        _owner = None
                        _session_started_at = None
                        _close_pty_master()
            asyncio.create_task(_clear())

        def _pty_reader_thread(master_fd: int, loop: asyncio.AbstractEventLoop) -> None:
            """Blocking PTY reader on a plain thread (immune to SSE close)."""
            buf = b""
            try:
                while True:
                    chunk = os.read(master_fd, 65536)
                    if not chunk:
                        break
                    if not ready_event.is_set():
                        ready_event.set()
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        text = raw.decode("utf-8", errors="replace").rstrip("\r")
                        loop.call_soon_threadsafe(_deliver, session_id, ("stdout", text))
            except OSError:
                pass
            if buf:
                text = buf.decode("utf-8", errors="replace").rstrip("\r")
                loop.call_soon_threadsafe(_deliver, session_id, ("stdout", text))

        async def login_watcher(proc: asyncio.subprocess.Process) -> None:
            """The authoritative agy success signal: poll the token file. The
            instant login writes it, kill agy (skip the throwaway prompt) and
            report success. Bound to a specific ``proc`` so it stays correct across
            respawns."""
            while True:
                await asyncio.sleep(1.0)
                if has_usable_login():
                    if proc.returncode is None:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                    _deliver(session_id, ("login_ok", 0))
                    return
                if proc.returncode is not None:
                    return

        async def wait_done(proc: asyncio.subprocess.Process) -> None:
            global _last_completed_session_id, _last_completed_ok
            returncode = await proc.wait()
            ok = returncode == 0 or has_usable_login()
            _last_completed_session_id = session_id
            _last_completed_ok = ok
            _deliver(session_id, ("exit", 0 if ok else (returncode or 1)))
            clear_session(proc)

        async def _spawn_login() -> Optional[asyncio.subprocess.Process]:
            """Spawn one throwaway ``agy -p`` under a PTY and wire its reader +
            watcher + wait tasks, publishing the session globals under the lock.
            Reused for the initial spawn and for each respawn when agy's own ~60s
            auth timer expires. Returns the process, or None if no PTY is available."""
            global _process, _pty_master, _session_id, _ready, _queue, _auth_url, _started_at
            global _owner
            if not _pty_wanted():
                return None
            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")
            env.setdefault("AGY_CLI_DISABLE_AUTO_UPDATE", "1")
            # Don't let agy try to open a browser on the SERVER — the user opens the
            # URL on their own machine (OOB paste-code flow).
            env["BROWSER"] = "true"
            env["DISPLAY"] = ""
            master_fd, slave_fd = _pty.openpty()
            try:
                _set_pty_winsize(slave_fd)
                _disable_pty_echo(slave_fd)
                proc = await asyncio.create_subprocess_exec(
                    agy_path, "-p", "hello",
                    "--model", _TRIGGER_MODEL,
                    "--dangerously-skip-permissions",
                    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, env=env,
                )
            except BaseException:
                for fd in (slave_fd, master_fd):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                raise
            os.close(slave_fd)
            async with _lock:
                # INTERLOCK. Between creating `proc` above and taking this lock, a
                # newer stream may have adopted the session. Publishing now would
                # (a) leave two live agy processes at the respawn boundary — the
                # loser's `finally` never runs because the proxy swallows the
                # disconnect, so the reject would re-arm; and (b) let
                # _close_pty_master() below close a global fd the other generator
                # is concurrently reinstalling, and fd numbers get recycled, so the
                # reader thread could end up on a recycled descriptor.
                # `_owner is None` is NOT supersession: clear_session releases it on
                # the respawn path, and we are then free to re-claim it below.
                if _owner is not None and _owner is not owner:
                    try:
                        os.close(master_fd)  # slave_fd is already closed above
                    except OSError:
                        pass
                    try:
                        proc.kill()  # clean up the agy we just spawned; never orphan it
                    except ProcessLookupError:
                        pass
                    return None
                # Close the PREVIOUS master before publishing the new one. On the
                # respawn path clear_session() usually gets there first (its
                # `_process is proc` guard still matches), but if it loses the race to
                # this spawn that guard stops matching and the old master would leak.
                # Defensive and idempotent: a no-op when already closed.
                _close_pty_master()
                _owner = owner
                _process = proc
                _pty_master = master_fd
                _session_id = session_id
                _ready = ready_event
                _queue = queue
                _auth_url = None
                _started_at = time.monotonic()
            threading.Thread(
                target=_pty_reader_thread,
                args=(master_fd, asyncio.get_running_loop()),
                daemon=True, name=f"agy-connect-{session_id[:8]}",
            ).start()
            asyncio.create_task(wait_done(proc))
            asyncio.create_task(login_watcher(proc))
            return proc

        try:
            async with _lock:
                # ATTACH, mirroring the proven reference (claude_setup_token.py:572-599).
                # A second connect is NOT an error: the dialog re-mounts, or the user
                # closes and reopens it. Crucially the xo-swarm proxy passes no
                # `signal` (app/api/antigravity/stream/route.ts:27 — identical to the
                # claude proxy), so closing the dialog never reaches us and the old
                # stream stays live. Rejecting therefore locked the user out for the
                # FULL CONNECT_TIMEOUT_SECONDS budget (measured: 3 chained agy spawns).
                # Taking the session over keeps the same session_id, agy and PTY, so a
                # code pasted against the original session_id still completes. Only the
                # consumer queue moves (last stream wins); the superseded stream sees
                # the owner poll below and ends with a terminal event.
                # `_session_id is not None` matters: at the respawn boundary
                # clear_session may have released the session while its generator is
                # still mid-respawn — there is nothing coherent to adopt, so we start
                # fresh and the _spawn_login interlock makes that generator kill its
                # own spawn rather than leaving two live agy processes.
                if _owner is not None and _session_id is not None:
                    _owner = owner
                    adopted = True
                    session_id = _session_id
                    adopted_url = _auth_url
                    old_queue = _queue
                    _queue = queue
                    # Hand over anything the previous stream never consumed, so an
                    # unconsumed auth_url or exit isn't stranded in a dead queue
                    # (claude_setup_token.py:592-599).
                    if old_queue is not None:
                        while True:
                            try:
                                queue.put_nowait(old_queue.get_nowait())
                            except asyncio.QueueEmpty:
                                break
                    # Reuse the live agy; a session caught at the respawn boundary has
                    # none, so we spawn for ourselves below rather than waiting on a
                    # queue that nothing will feed.
                    process = _process if (_process is not None and _process.returncode is None) else None
                    # Deliberately NOT touched on this path:
                    #  - `_ready`: the live reader thread sets the ORIGINAL Event;
                    #    installing ours would hang the callback's readiness gate.
                    #  - `_code_submitted`: a re-mounted dialog must not re-arm the
                    #    respawn and kill an agy that is mid token-exchange (P1.2).
                else:
                    _owner = owner
                    _session_started_at = time.monotonic()
                    _code_submitted = False
                    if _process is not None:
                        _close_pty_master()

            if process is None:
                process = await _spawn_login()
                if process is None:
                    # Either no PTY, or we lost the interlock race while spawning.
                    # `_owner is None` means the session was merely released, not
                    # stolen — that is not supersession, so use the same two-part
                    # predicate as every other ownership check.
                    superseded = _owner is not None and _owner is not owner
                    yield _err_event(
                        _SUPERSEDED_MESSAGE if superseded
                        else 'PTY unavailable; antigravity login needs a terminal.'
                    )
                    return

            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
            if adopted and adopted_url:
                # Replay the URL so a reopened dialog renders immediately, and arm the
                # respawn guard correctly. If a respawn had already reset `_auth_url`,
                # leave auth_url_sent False — self-healing: the new URL arrives on the
                # swapped queue and is extracted below.
                auth_url_sent = True
                yield f"data: {json.dumps({'type': 'auth_url', 'url': adopted_url})}\n\n"
            # The overall budget spans ALL respawns: each agy self-times-out at ~60s,
            # and we refresh the URL on timeout so a slow-but-valid user (opening the
            # link, signing in, pasting the code) isn't hard-failed by that 60s.
            # Anchored to the ORIGINAL session start so reattaching cannot extend it.
            overall_deadline = (_session_started_at or time.monotonic()) + CONNECT_TIMEOUT_SECONDS

            while True:
                if _owner is not None and _owner is not owner:
                    # A newer stream adopted the session (last one wins). End THIS
                    # stream only — the login itself is untouched and a paste against
                    # the same session_id still works. Dropping our local `process`
                    # reference is the whole mechanism: the `finally` is guarded on it,
                    # so we exit without killing the agy the adopter now owns and
                    # without clearing globals out from under it.
                    process = None
                    yield _err_event(_SUPERSEDED_MESSAGE)
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    if _owner is not None and _owner is not owner:
                        # Superseded while we idled — loop so the poll above handles
                        # it. Falling through to the deadline check would kill the
                        # adopted agy.
                        continue
                    if time.monotonic() >= overall_deadline:
                        if process and process.returncode is None:
                            process.kill()
                        clear_session(process)
                        yield _err_event('Antigravity login timed out')
                        break
                    yield ": heartbeat\n\n"
                    continue

                kind, value = item

                if kind in ("login_ok", "exit"):
                    ok = has_usable_login() or (kind == "login_ok")
                    if ok:
                        clear_session(process)
                        # COMPAT SHIM — the deployed xo-swarm dialog flips to success ONLY by
                        # scraping this exact string from a stdout event
                        # (setup-antigravity-dialog.tsx:29-32 @ HEAD 2815131). agy never prints
                        # it (verified: 0 hits in the binary). Re-added after c3bacaa removed it
                        # on the premise of a purpose-built frontend that was never shipped.
                        # Must precede the `done` yield: `done` triggers es.close(), so anything
                        # emitted after it is never read.
                        # REMOVE ONLY AFTER xo-swarm keys success off
                        # `data.type === "done" && data.ok === true`.
                        yield f"data: {json.dumps({'type': 'stdout', 'line': 'Authentication token created successfully'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'returncode': 0, 'ok': True})}\n\n"
                        break
                    if kind == "exit":
                        # agy exited without writing a token. If it had reached the
                        # auth prompt (URL shown) and the user hasn't pasted a code
                        # yet, this is agy's own ~60s auth timer expiring — respawn a
                        # fresh agy + URL (up to overall_deadline) rather than
                        # hard-failing a user still completing the browser flow. A URL
                        # never shown ⇒ a genuine startup failure ⇒ don't respawn-storm.
                        if auth_url_sent and not _code_submitted and time.monotonic() < overall_deadline:
                            auth_url_sent = False
                            new_proc = await _spawn_login()
                            if new_proc is not None:
                                process = new_proc
                                yield f"data: {json.dumps({'type': 'stdout', 'line': 'Sign-in link expired — issuing a fresh one…'})}\n\n"
                                continue
                            if _owner is not None and _owner is not owner:
                                # The interlock declined our respawn and killed it: a
                                # newer stream owns the session. Let the poll at the
                                # top of the loop end this stream cleanly.
                                process = None
                                continue
                        clear_session(process)
                        yield _err_event('Antigravity login failed. Please try again.')
                        break
                    # login_ok without a usable token (shouldn't happen) — keep waiting.
                    continue

                # kind == "stdout": forward escape-free text; emit the URL once per attempt.
                clean = _strip_ansi(value)
                stripped = clean.strip()
                if not auth_url_sent:
                    url = _extract_auth_url(clean)
                    if url:
                        _auth_url = url
                        auth_url_sent = True
                        yield f"data: {json.dumps({'type': 'auth_url', 'url': url})}\n\n"
                        continue
                if stripped:
                    yield f"data: {json.dumps({'type': 'stdout', 'line': clean})}\n\n"

        except FileNotFoundError:
            if process is not None:
                clear_session(process)
            yield _err_event(f'agy CLI not found: {agy_path}')
        except Exception as e:
            if process is not None:
                clear_session(process)
            yield _err_event(str(e))
        finally:
            # On generator close (SSE client disconnect) or any exit, tear down the
            # current throwaway agy if it is still alive, so a login process never
            # lingers to its own ~60s timeout. (A succeeded run is already dead here —
            # login_watcher killed it — so this only fires on disconnect/abort.)
            #
            # `process is None` here means we were superseded: the adopter owns the
            # agy now and must NOT be torn down.
            #
            # This kill is deliberately NOT the reference's DETACH
            # (claude_setup_token.py:812-818). Claude can leave its CLI running on
            # disconnect because it has no respawn loop; we do, so an unread-but-live
            # generator could keep spawning fresh agy processes to the deadline.
            # ATTACH is what makes DETACH unnecessary: a re-mounted dialog adopts the
            # live session instead of needing it to survive an unnoticed disconnect.
            # NOTE: if `signal: request.signal` is ever added to the xo-swarm proxy
            # (route.ts:27), this calculus flips — every dialog close would then kill
            # agy and invalidate the user's in-flight OAuth code, and this must become
            # DETACH plus a standalone watchdog.
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                clear_session(process)
            elif _owner is owner:
                # No process to clear, but we still hold the session (e.g. PTY
                # unavailable). Release it so the next connect starts fresh rather
                # than adopting a session with no generator behind it.
                _owner = None
                _session_started_at = None

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
