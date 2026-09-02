"""
Connect Claude — drive ``claude auth login --claudeai`` and stream its output via SSE.

The endpoint paths (``/connect/claude-code`` and ``/connect/claude-code/callback``)
and the SSE event shapes are unchanged for frontend backward-compatibility; only
the backend mechanism changed. We no longer scrape or persist any token: the CLI
writes its own self-refreshing ``~/.claude/.credentials.json`` on success, and the
authoritative "connected" check stays ``claude auth status --json`` → ``loggedIn``.

Flow:
  POST /connect/claude-code (SSE)
    1. Spawn ``claude auth login --claudeai`` under a PTY. Its manual-code prompt
       reads stdin via readline; a plain stdin pipe was ignored in practice, so we
       drive a PTY master.
    2. The CLI prints a clean authorize URL ("…visit: https://claude.com/cai/…");
       we extract it and emit it as the ``auth_url`` event.
    3. The user opens the URL, authorizes, and Claude's page displays a
       ``code#state`` blob to paste.
  POST /connect/claude-code/callback
    4. Normalize the pasted value to ``code#state`` and write it (plain bytes + CR)
       to the PTY master. An "Invalid code…" line is *retryable* — the CLI keeps
       waiting for another line, so we keep the session alive for another paste.
       It is published as a structured ``code_rejected`` event (``retryable: true``)
       as well as the raw stdout line, so a client never has to pattern-match the
       CLI's prose to know the paste failed.
    5. On exit 0 (reached only after the CLI writes credentials and prints
       "Login successful.") we emit ``done`` directly — we do NOT gate it on an
       extra ``claude auth status --json`` spawn, which would delay the event.
       For older frontends that detect success by scraping stdout, a recognized
       success line is also emitted as ``stdout``. On exit 1 we surface the CLI's
       "Login failed: …" line as an ``error``.
  POST /connect/claude-code?force=1
    Abandon any live session (kill the CLI, close the PTY, clear state) and
    start a fresh one instead of attaching to it — the "Try again" escape
    hatch for a wedged login.
  DELETE /connect/claude-code
    Explicitly cancel the active session, if any. Idempotent: 200 even when
    nothing is active.

On Unix the CLI is spawned with a pseudo-terminal (PTY). Set
CLAUDE_SETUP_TOKEN_USE_PTY=0 to force the legacy pipe mode.
"""

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from typing import Optional, AsyncGenerator
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

# PTY lets the CLI read its manual-code prompt from a real terminal (a plain
# stdin pipe was ignored in practice and the process blocked).
try:
    import fcntl
    import struct
    import termios

    import pty as _pty

    _HAS_PTY = hasattr(_pty, "openpty") and sys.platform != "win32"
except ImportError:
    _HAS_PTY = False

from fastapi import APIRouter, HTTPException
from services.cowork_agent.adapters.claude_code.auth_state import clear_auth_failure
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


# Strip CSI, OSC (incl. the OSC-8 hyperlink the CLI may wrap a URL in) and
# two-char escapes so terminal bytes never reach the frontend or corrupt the
# captured authorize URL. The nF-class rule strips the charset-designation reset
# ESC ( B ("\x1b(B") that the 2-char rule misses (its intermediate byte "(" 0x28
# is outside the @-_ range).
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_2CHAR_RE = re.compile(r"\x1b[@-Z\\-_]")
_ANSI_NF_RE = re.compile(r"\x1b[\x20-\x2f]+[\x30-\x7e]")


def _strip_ansi(line: str) -> str:
    """Remove ANSI/OSC escape sequences (and stray BEL) from CLI output."""
    line = _ANSI_OSC_RE.sub("", line)
    line = _ANSI_CSI_RE.sub("", line)
    line = _ANSI_NF_RE.sub("", line)
    line = _ANSI_2CHAR_RE.sub("", line)
    return line.replace("\x07", "")


# The CLI prints the authorize URL on a "…visit: <url>" line. Capture the URL
# after the "visit:" marker so we stay host-agnostic: Claude Code v2.1.195 uses
# https://claude.com/cai/oauth/authorize (older builds used claude.ai/oauth/...).
_VISIT_URL_RE = re.compile(r"visit:\s*(https://[^\s\"'<>\x00-\x1f]+)", re.IGNORECASE)
# Fallback: a bare OAuth authorize URL anywhere in the text (any host/path).
_AUTH_URL_RE = re.compile(r"https://[^\s\"'<>\x00-\x1f]+/oauth/authorize\?[^\s\"'<>\x00-\x1f]+")


def _extract_auth_url(text: str) -> Optional[str]:
    """Return a clean, param-deduplicated OAuth authorize URL from `text`, or None.

    Prefers the URL printed after the "visit:" marker; falls back to a bare
    authorize URL. Rebuilds the query keeping the first value of each param so a
    redraw that repeats params can't produce a doubled link.
    """
    match = _VISIT_URL_RE.search(text)
    raw: Optional[str] = match.group(1) if match else None
    if raw is None:
        fallback = _AUTH_URL_RE.search(text)
        raw = fallback.group(0) if fallback else None
    if not raw:
        return None
    second = raw.find("https://", len("https://"))  # drop a concatenated redraw
    if second != -1:
        raw = raw[:second]
    split = urlsplit(raw)
    deduped: dict[str, str] = {}
    for key, value in parse_qsl(split.query, keep_blank_values=True):
        value = "".join(ch for ch in value if ord(ch) >= 0x20)  # drop leftover garbage
        if key and key not in deduped:
            deduped[key] = value
    if not deduped:
        return None
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(deduped), ""))


def _resolve_claude_cli_path() -> str:
    """Resolve Claude CLI path from env or PATH (avoids circular import from server)."""
    path = (os.getenv("CLAUDE_CLI_PATH") or "claude").strip()
    if not os.path.isabs(path):
        found = shutil.which(path)
        if found:
            return found
    return path


CLAUDE_SETUP_TOKEN_TIMEOUT_SECONDS = int(os.getenv("CLAUDE_SETUP_TOKEN_TIMEOUT", "300"))
# Seconds of SSE silence between keepalive comments (env override is for tests).
SSE_HEARTBEAT_SECONDS = int(os.getenv("CLAUDE_SETUP_TOKEN_HEARTBEAT", "15"))


def _mark_onboarding_complete() -> None:
    """Ensure ``~/.claude.json`` records ``hasCompletedOnboarding: true``.

    The interactive CLI gates its first-run UI on this onboarding state, not on
    credentials: with a valid ``~/.claude/.credentials.json`` but no completed
    onboarding, the TUI still walks theme selection and a "Select login method"
    screen (verified on v2.1.201 with isolated homes), so on a fresh workspace a
    terminal ``claude`` looks logged-out right after a successful Connect
    Claude. ``claude auth login`` writes only the credentials file, so we merge
    the flag here on login success. Existing keys are preserved; an unreadable
    file is left untouched (the CLI owns this file — never clobber it).
    """
    path = os.path.expanduser("~/.claude.json")
    data: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        print(f"[setup-token] ~/.claude.json unreadable ({e}); leaving it untouched")
        return
    if data.get("hasCompletedOnboarding") is True:
        return
    data["hasCompletedOnboarding"] = True
    tmp_path = f"{path}.setup-token.tmp"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
        print("[setup-token] marked onboarding complete in ~/.claude.json")
    except OSError as e:
        print(f"[setup-token] could not update ~/.claude.json: {e}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

_setup_token_lock = asyncio.Lock()
_setup_token_process: Optional[asyncio.subprocess.Process] = None
_setup_token_stdin: Optional[asyncio.StreamWriter] = None
# When using a PTY, stdin is not a StreamWriter; we write the pasted code here.
_setup_token_pty_master: Optional[int] = None
_setup_token_session_id: Optional[str] = None
_setup_token_ready: Optional[asyncio.Event] = None
# Remember the most recently finished session so a frontend retry / double-submit
# of the callback *after* the login already completed returns 200 (idempotent)
# instead of a 409 the UI would render as a failure.
_last_completed_session_id: Optional[str] = None
_last_completed_ok: bool = False
# The CURRENT SSE consumer's queue. A second POST /connect/claude-code while a
# login is in flight ATTACHES to the session (same session_id/CLI/PTY) instead
# of orphaning it: it swaps this queue (last stream wins) and the superseded
# stream ends itself with a terminal event. Producers route through _deliver().
_setup_token_queue: Optional[asyncio.Queue] = None
# Cached authorize URL so an attaching stream can replay it immediately.
_setup_token_auth_url: Optional[str] = None
# monotonic() at spawn — the overall deadline stays anchored to the ORIGINAL
# session start even across attaches.
_setup_token_started_at: Optional[float] = None


def _deliver(session_id: str, item: tuple) -> None:
    """Route a producer event (PTY/pipe reader, exit watcher) to the current
    consumer queue. Runs on the event loop (via call_soon_threadsafe from the
    reader thread). Stragglers from a session that is no longer the active one
    (a finished session that was taken over) are dropped."""
    if _setup_token_session_id == session_id and _setup_token_queue is not None:
        _setup_token_queue.put_nowait(item)


def _pty_wanted() -> bool:
    flag = os.getenv("CLAUDE_SETUP_TOKEN_USE_PTY", "1").strip().lower()
    return flag not in ("0", "false", "no", "off") and _HAS_PTY


def _set_pty_winsize(slave_fd: int, rows: int = 24, cols: int = 1000) -> None:
    """Best-effort terminal size for the child. A wide width stops any wrapping
    of the long authorize URL across lines (which would corrupt extraction)."""
    if not _HAS_PTY:
        return
    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def _disable_pty_echo(slave_fd: int) -> None:
    """Turn off terminal echo on the child PTY. In the default (cooked) line
    discipline the master would otherwise echo back everything written to it —
    including the pasted ``code#state`` — which would put the one-time
    authorization code into server logs and the SSE stream. The CLI's readline
    still receives the input; only the echo is suppressed. Best-effort."""
    if not _HAS_PTY:
        return
    try:
        attrs = termios.tcgetattr(slave_fd)
        attrs[3] = attrs[3] & ~termios.ECHO  # lflags &= ~ECHO
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except OSError:
        pass


async def _write_setup_token_stdin(text_bytes: bytes) -> None:
    """Write the pasted ``code#state`` to the running ``claude auth login`` CLI.

    The CLI reads its manual-code prompt with ``readline`` on stdin and consumes
    a single line. Unlike the old Ink setup-token UI this is NOT bracketed paste:
    write the bytes plainly and submit with Enter (``\\r``). A plain stdin pipe was
    ignored in practice, so the PTY master is the real channel.
    """
    global _setup_token_stdin, _setup_token_pty_master
    if _setup_token_pty_master is not None:
        print(f"[setup-token] writing code to PTY master ({len(text_bytes)}B + CR)")
        written = await asyncio.to_thread(os.write, _setup_token_pty_master, text_bytes + b"\r")
        print(f"[setup-token] PTY os.write returned {written}")
        return
    if _setup_token_stdin is not None:
        print(f"[setup-token] writing code to PIPE stdin ({len(text_bytes)}B + CR)")
        _setup_token_stdin.write(text_bytes + b"\r")
        await _setup_token_stdin.drain()
        return
    print("[setup-token] ERROR: no stdin channel available")
    raise RuntimeError("No setup-token stdin channel (internal error)")


def _close_setup_token_pty_master() -> None:
    global _setup_token_pty_master
    if _setup_token_pty_master is not None:
        fd = _setup_token_pty_master
        _setup_token_pty_master = None
        try:
            os.close(fd)
        except OSError:
            pass


# Canonical routes are /connect/*. The pre-rename /claude/setup-token* paths are
# kept as hidden legacy aliases (include_in_schema=False) so an un-migrated
# xo-swarm frontend keeps working — remove them once all clients call /connect/*.
router = APIRouter(tags=["claude-setup-token"])


class ClaudeSetupTokenCallbackBody(BaseModel):
    """Body for pasting the OAuth code shown by Claude's authorize page."""
    code: str  # Paste the full string from the browser, e.g. "code#state"
    session_id: str  # Must match the session_id emitted by the setup-token SSE stream


def _normalize_callback_code(raw_value: str) -> str:
    """
    Accept multiple callback formats and normalize to the `code#state` format
    that ``claude auth login`` expects in its manual-entry prompt (it does
    ``m.trim().split("#")`` and requires BOTH halves).
    """
    value = (raw_value or "").strip()
    if not value:
        return value

    # Full redirect URL -> extract query params
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        if code and state:
            return f"{code}#{state}"
        if code:
            return code
        return value

    # querystring payload -> convert to code#state
    if "code=" in value and "state=" in value:
        params = parse_qs(value)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        if code and state:
            return f"{code}#{state}"
        if code:
            return code
    return value


@router.post("/connect/claude-code/callback")
@router.post("/claude/setup-token/callback", include_in_schema=False)  # legacy alias
async def claude_setup_token_callback(body: ClaudeSetupTokenCallbackBody):
    """
    Send the pasted OAuth code to the running ``claude auth login`` process.

    After the user authorizes in the browser, Claude's page shows a ``code#state``
    string. The frontend posts it here; we normalize it and write it to the CLI
    (via the PTY) so the login can complete. An "Invalid code…" line from the CLI
    is retryable — the session stays alive so the user can paste again.

    Requires `session_id` from the SSE stream to ensure the callback targets the
    correct running session.
    """
    global _setup_token_process, _setup_token_stdin, _setup_token_pty_master, _setup_token_session_id, _setup_token_ready
    global _setup_token_queue, _setup_token_auth_url, _setup_token_started_at

    print(f"[setup-token] callback received (session_id={body.session_id})")

    if _setup_token_process is None or _setup_token_session_id is None:
        # The session may have already finished. If this is a retry of a session
        # that completed successfully (the SSE `done` can race a frontend that
        # re-posts the callback), report success idempotently instead of a 409
        # the UI would render as a failure.
        if body.session_id == _last_completed_session_id and _last_completed_ok:
            print("[setup-token] callback for an already-succeeded session → 200 (idempotent)")
            return {"ok": True, "message": "Login already completed", "session_id": body.session_id}
        print(
            f"[setup-token] callback rejected: no active session "
            f"(last_completed={_last_completed_session_id}, last_ok={_last_completed_ok} — "
            f"a mismatching last_completed here means the server restarted mid-login)"
        )
        raise HTTPException(
            status_code=409,
            detail="No setup-token session active. Start one with POST /connect/claude-code first.",
        )

    if body.session_id != _setup_token_session_id:
        active_age = time.monotonic() - (_setup_token_started_at or time.monotonic())
        print(
            f"[setup-token] callback rejected: session mismatch "
            f"(active={_setup_token_session_id} age={active_age:.0f}s, got={body.session_id})"
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Session mismatch. The active session is '{_setup_token_session_id}' "
                f"but callback provided '{body.session_id}'. "
                "Start a new setup-token session and use the session_id from its SSE stream."
            ),
        )

    ready_event = _setup_token_ready
    if ready_event is not None and not ready_event.is_set():
        print("[setup-token] callback waiting for CLI readiness...")
        try:
            await asyncio.wait_for(ready_event.wait(), timeout=30.0)
            print("[setup-token] CLI is ready for input")
        except asyncio.TimeoutError:
            print("[setup-token] callback rejected: CLI not ready within 30s")
            raise HTTPException(
                status_code=504,
                detail="CLI process did not become ready in time. It may have failed to start.",
            )

    async with _setup_token_lock:
        no_input = _setup_token_stdin is None and _setup_token_pty_master is None
        if no_input or _setup_token_process is None:
            print("[setup-token] callback rejected: session gone after ready-wait")
            raise HTTPException(
                status_code=409,
                detail="Setup-token session is no longer active (it may have been cleaned up).",
            )
        if body.session_id != _setup_token_session_id:
            print("[setup-token] callback rejected: session mismatch (post-lock)")
            raise HTTPException(
                status_code=409,
                detail="Session changed while waiting. Please start a new setup-token session.",
            )
        if _setup_token_process.returncode is not None:
            _setup_token_process = None
            _setup_token_stdin = None
            _setup_token_session_id = None
            _setup_token_ready = None
            _setup_token_queue = None
            _setup_token_auth_url = None
            _setup_token_started_at = None
            _close_setup_token_pty_master()
            print("[setup-token] callback rejected: process already finished")
            raise HTTPException(
                status_code=409,
                detail="Setup-token process already finished. Start a new session if needed.",
            )
        try:
            normalized = _normalize_callback_code(body.code)
            print(f"[setup-token] callback normalized (raw_len={len((body.code or '').strip())} normalized_len={len(normalized)})")
            text_payload = normalized.encode("utf-8")
            pid = _setup_token_process.pid if _setup_token_process else None
            print(f"[setup-token] sending code ({len(text_payload)}B) to CLI (pid={pid})")
            await _write_setup_token_stdin(text_payload)
            print("[setup-token] callback payload sent successfully")
            return {"ok": True, "message": "Code sent to CLI", "session_id": _setup_token_session_id}
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            _setup_token_process = None
            _setup_token_stdin = None
            _setup_token_session_id = None
            _setup_token_ready = None
            _close_setup_token_pty_master()
            print(f"[setup-token] callback write FAILED: {e}")
            raise HTTPException(status_code=410, detail=f"Process stdin closed: {e}")


@router.post("/connect/claude-code")
@router.post("/claude/setup-token", include_in_schema=False)  # legacy alias
async def claude_setup_token(force: bool = False):
    """
    Run ``claude auth login --claudeai`` and stream stdout/stderr via SSE.

    The CLI prints an authorize URL; the frontend shows it so the user can open it
    and authorize. Claude's page then shows a ``code#state`` string — the user
    copies it and the frontend posts it to POST /connect/claude-code/callback so we
    can complete the login. On success the CLI writes its own self-refreshing
    ``~/.claude/.credentials.json``; we persist nothing.

    Uses a PTY on Unix by default so the CLI's manual-code prompt reads input.

    ``force`` is a query param, never a body — this endpoint keeps accepting
    requests that send none. When true and a session is already live, that
    session is abandoned (CLI killed, PTY closed, globals cleared) before a
    fresh one is spawned, instead of attaching to it. Absent/falsy, behaviour
    is byte-identical to before: attach to a live session if one exists, else
    spawn.
    """
    global _setup_token_process, _setup_token_stdin, _setup_token_pty_master
    cli_path = _resolve_claude_cli_path()
    print(f"[setup-token] session start requested (cli_path={cli_path}, force={force})")

    async def generate() -> AsyncGenerator[str, None]:
        global _setup_token_process, _setup_token_stdin, _setup_token_pty_master, _setup_token_session_id, _setup_token_ready
        global _setup_token_queue, _setup_token_auth_url, _setup_token_started_at
        queue: asyncio.Queue = asyncio.Queue()
        process: Optional[asyncio.subprocess.Process] = None
        attach = False
        auth_url_sent = False
        compat_success_sent = False  # legacy success shim emitted early (see below)
        last_failure_line: Optional[str] = None  # most recent "Login failed: …" line
        session_id = str(uuid.uuid4())
        ready_event = asyncio.Event()

        async def read_stdout(proc: asyncio.subprocess.Process) -> None:
            if proc.stdout is None:
                return
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                if not ready_event.is_set():
                    ready_event.set()
                    print("[setup-token] CLI ready (first stdout received)")
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                _deliver(session_id, ("stdout", text))

        async def read_stderr(proc: asyncio.subprocess.Process) -> None:
            if proc.stderr is None:
                return
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                _deliver(session_id, ("stderr", text))

        def _pty_reader_thread(master_fd: int, loop: asyncio.AbstractEventLoop) -> None:
            """Read PTY master in a plain OS thread — completely decoupled from asyncio.

            Previous approaches (asyncio.to_thread, loop.add_reader) stopped
            reading after the SSE StreamingResponse context closed, causing the
            PTY output buffer to fill and the CLI to deadlock on stdout write.
            A plain thread with blocking os.read() is immune to all of that.
            """
            buf = b""
            try:
                while True:
                    chunk = os.read(master_fd, 65536)
                    if not chunk:
                        break
                    if not ready_event.is_set():
                        ready_event.set()
                        print("[setup-token] CLI ready (first PTY output received)")
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        text = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                        stripped = _strip_ansi(text).strip()
                        if stripped:
                            print(f"[setup-token] PTY> {stripped[:200]}")
                        loop.call_soon_threadsafe(_deliver, session_id, ("stdout", text))
            except OSError as e:
                print(f"[setup-token] PTY read ended: {e}")
            if buf:
                tail = buf.decode("utf-8", errors="replace").rstrip("\r")
                if tail:
                    stripped = _strip_ansi(tail).strip()
                    if stripped:
                        print(f"[setup-token] PTY> {stripped[:200]}")
                    loop.call_soon_threadsafe(_deliver, session_id, ("stdout", tail))
            print("[setup-token] PTY reader thread exiting")

        async def wait_done(proc: asyncio.subprocess.Process) -> None:
            global _last_completed_session_id, _last_completed_ok
            returncode = await proc.wait()
            if returncode < 0:
                # Negative = killed by a signal, i.e. something OUTSIDE the flow
                # (server restart via pkill -P, deploy, OOM) — the discriminator
                # for "error appeared mid-login for no visible reason".
                print(
                    f"[setup-token] process exited (returncode={returncode} — "
                    f"killed by signal {-returncode}; external restart/kill?)"
                )
            else:
                print(f"[setup-token] process exited (returncode={returncode})")
            # Record completion here — wait_done() always runs on process exit,
            # whereas the SSE loop's `done` branch is skipped if the client
            # disconnected exactly at completion. Setting the idempotency flags
            # here keeps a callback retry after such a disconnect a 200, not a 409.
            _last_completed_session_id = session_id
            _last_completed_ok = returncode == 0
            if returncode == 0:
                # The CLI wrote credentials but not the onboarding state the
                # interactive TUI checks — without this, terminal `claude` on a
                # fresh workspace still shows its login screen.
                await asyncio.to_thread(_mark_onboarding_complete)
            _deliver(session_id, ("done", returncode))
            # Also clean up directly in case the SSE stream already closed
            # and nobody is consuming the queue anymore.
            clear_session()

        def clear_session() -> None:
            async def _clear():
                global _setup_token_process, _setup_token_stdin, _setup_token_pty_master, _setup_token_session_id, _setup_token_ready
                global _setup_token_queue, _setup_token_auth_url, _setup_token_started_at
                async with _setup_token_lock:
                    if _setup_token_process is process:
                        _setup_token_process = None
                        _setup_token_stdin = None
                        _setup_token_session_id = None
                        _setup_token_ready = None
                        _setup_token_queue = None
                        _setup_token_auth_url = None
                        _setup_token_started_at = None
                        _close_setup_token_pty_master()
                        print("[setup-token] session cleared")

            asyncio.create_task(_clear())

        try:
            async with _setup_token_lock:
                if (
                    _setup_token_process is not None
                    and _setup_token_process.returncode is None
                    and not force
                ):
                    # A login is already in flight (dialog re-mount, double
                    # submit, or a retry after an SSE drop). ATTACH to it
                    # instead of orphaning it: same session_id, same CLI, same
                    # PTY — a paste against the original session_id still
                    # works. Only the consumer queue moves (last stream wins);
                    # the superseded stream notices and ends with a terminal
                    # event.
                    attach = True
                    process = _setup_token_process
                    session_id = _setup_token_session_id or session_id
                    age = time.monotonic() - (_setup_token_started_at or time.monotonic())
                    print(
                        f"[setup-token] attaching stream to active session "
                        f"{session_id} (pid={process.pid}, age={age:.0f}s)"
                    )
                    # Hand over events the previous stream never consumed (its
                    # client may have dropped between produce and consume) —
                    # otherwise an unconsumed auth_url or even a `done` would be
                    # stranded in the abandoned queue.
                    old_queue = _setup_token_queue
                    _setup_token_queue = queue
                    if old_queue is not None:
                        while True:
                            try:
                                queue.put_nowait(old_queue.get_nowait())
                            except asyncio.QueueEmpty:
                                break
                else:
                    if _setup_token_process is not None:
                        if _setup_token_process.returncode is None:
                            # force=1 ("Try again", D6): a login is live but the
                            # caller wants to abandon it rather than attach.
                            # Kill it here; the PTY-fd close just below (shared
                            # with the "finished" branch) plus the fresh-spawn
                            # assignments a few lines down overwrite every
                            # remaining module global, so no separate
                            # clear-globals helper is needed. The old session's
                            # own wait_done() task still fires later and calls
                            # clear_session(), but that closure's
                            # "if _setup_token_process is process" guard
                            # compares against THIS process object — which by
                            # then no longer matches _setup_token_process (the
                            # new session) — so it safely no-ops instead of
                            # tearing down the session we're about to create.
                            print(
                                f"[setup-token] force=1: abandoning live session "
                                f"{_setup_token_session_id} "
                                f"(pid={_setup_token_process.pid})"
                            )
                            _setup_token_process.kill()
                        else:
                            # A finished session that was never consumed/cleared —
                            # take it over cleanly instead of leaking its PTY fd.
                            print(
                                f"[setup-token] taking over finished session "
                                f"{_setup_token_session_id} "
                                f"(returncode={_setup_token_process.returncode})"
                            )
                        _close_setup_token_pty_master()
                    env = os.environ.copy()
                    # The login must not be steered by ambient token env: the workspace pod
                    # injects ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN (real values in
                    # prod, empty in dev), and the CLI weighs env tokens above the native
                    # login. `claude auth login` performs its own OAuth exchange and writes
                    # credentials.json regardless, so strip the token vars for parity with
                    # the chat path's sanitized env.
                    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
                        env.pop(key, None)
                    # Pin TERM so the CLI's terminal output is deterministic regardless of
                    # how the API was launched. ttyd sets TERM=xterm-256color (the known-good
                    # path); the Coder startup_script leaves it unset, which changes the
                    # escape sequences the CLI emits. setdefault (not a hardcode) supplies a
                    # value only when the launcher gave none, so an explicitly-set TERM is
                    # still respected.
                    env.setdefault("TERM", "xterm-256color")
                    use_pty = _pty_wanted()
                    print(f"[setup-token] spawning process (use_pty={use_pty}, TERM={env.get('TERM')})")
                    if use_pty:
                        master_fd, slave_fd = _pty.openpty()
                        try:
                            _set_pty_winsize(slave_fd)
                            _disable_pty_echo(slave_fd)  # keep the pasted code out of logs/SSE
                            process = await asyncio.create_subprocess_exec(
                                cli_path,
                                "auth",
                                "login",
                                "--claudeai",
                                stdin=slave_fd,
                                stdout=slave_fd,
                                stderr=slave_fd,
                                env=env,
                            )
                        except BaseException:
                            for fd in (slave_fd, master_fd):
                                try:
                                    os.close(fd)
                                except OSError:
                                    pass
                            raise
                        os.close(slave_fd)
                        _setup_token_process = process
                        _setup_token_stdin = None
                        _setup_token_pty_master = master_fd
                        _setup_token_session_id = session_id
                        _setup_token_ready = ready_event
                        _setup_token_queue = queue
                        _setup_token_auth_url = None
                        _setup_token_started_at = time.monotonic()
                        _reader_thread = threading.Thread(
                            target=_pty_reader_thread,
                            args=(master_fd, asyncio.get_running_loop()),
                            daemon=True,
                            name=f"pty-reader-{session_id[:8]}",
                        )
                        _reader_thread.start()
                        asyncio.create_task(wait_done(process))
                    else:
                        process = await asyncio.create_subprocess_exec(
                            cli_path,
                            "auth",
                            "login",
                            "--claudeai",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            stdin=asyncio.subprocess.PIPE,
                            env=env,
                        )
                        _setup_token_process = process
                        _setup_token_stdin = process.stdin
                        _setup_token_pty_master = None
                        _setup_token_session_id = session_id
                        _setup_token_ready = ready_event
                        _setup_token_queue = queue
                        _setup_token_auth_url = None
                        _setup_token_started_at = time.monotonic()
                        asyncio.create_task(read_stdout(process))
                        asyncio.create_task(read_stderr(process))
                        asyncio.create_task(wait_done(process))

            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"
            if attach:
                cached_url = _setup_token_auth_url
                if cached_url:
                    # Replay the authorize URL so the attaching dialog can render
                    # immediately (still at most one auth_url per stream).
                    auth_url_sent = True
                    yield f"data: {json.dumps({'type': 'auth_url', 'url': cached_url})}\n\n"
                print(f"[setup-token] stream attached (session_id={session_id}, url_replayed={auth_url_sent})")
            else:
                print(f"[setup-token] session started (session_id={session_id})")

            # Deadline stays anchored to the ORIGINAL session start, even for
            # streams that attached later.
            _deadline = (_setup_token_started_at or time.monotonic()) + CLAUDE_SETUP_TOKEN_TIMEOUT_SECONDS

            while True:
                if _setup_token_queue is not queue:
                    # A newer stream attached (last one wins), or the session was
                    # cleared/cancelled while this stream idled. End THIS stream with
                    # its terminal event.
                    #
                    # Which of those happened matters to the message: an attach
                    # takeover leaves the login alive (a paste against the same
                    # session_id still works), but force=1 and DELETE kill the process
                    # first — telling that user the session "is still active" would be
                    # a lie. `_setup_token_process is None` distinguishes them, since
                    # both teardown paths clear it before swapping the queue.
                    still_live = _setup_token_process is not None
                    reason = (
                        "This connection was superseded by a newer one; the login "
                        "session is still active."
                        if still_live
                        else "This login session was cancelled. Start a new one to "
                        "get a fresh sign-in link."
                    )
                    print(
                        f"[setup-token] stream superseded (session_id={session_id}, "
                        f"session_still_live={still_live})"
                    )
                    yield f"data: {json.dumps({'type': 'error', 'error': reason})}\n\n"
                    break
                # Checked at the TOP of every iteration — not only inside the
                # `except asyncio.TimeoutError` branch below — so a chatty CLI
                # that keeps handing us events faster than SSE_HEARTBEAT_SECONDS
                # can't defer this indefinitely (queue.get() would never time
                # out, so the old in-except check would never run).
                if time.monotonic() >= _deadline:
                    if process and process.returncode is None:
                        print("[setup-token] timed out; killing process")
                        process.kill()
                    clear_session()
                    yield f"data: {json.dumps({'type': 'error', 'error': 'Setup timed out'})}\n\n"
                    break
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=SSE_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    # Additive keepalive alongside the SSE comment frame above:
                    # a lossy intermediary proxy may not forward bare comment
                    # frames, so a data-framed event gives it something to pass
                    # through. New event type, so unmodified frontends (which
                    # only handle the existing `type`s) simply ignore it.
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
                    continue
                kind, value = item
                if kind == "done":
                    returncode = value
                    # `claude auth login` writes ~/.claude/.credentials.json and
                    # prints "Login successful." only on a successful exchange,
                    # then exits 0 — so exit 0 is authoritative. Emit `done`
                    # immediately rather than first blocking on a `claude auth
                    # status --json` subprocess: that extra `claude` spawn can take
                    # several seconds on a cold box, delaying this event past the
                    # frontend's "verifying" wait — the frontend then retried the
                    # callback (→ 409 → "failed") even though login had succeeded.
                    # The connected tile re-runs `claude auth status --json`
                    # authoritatively, so we don't gate the event on it here.
                    # (Completion/idempotency flags are recorded in wait_done(),
                    # the always-run path — see above.)
                    clear_session()
                    if returncode == 0:
                        print("[setup-token] login successful (exit 0)")
                        clear_auth_failure()
                        # Compat shim for the currently-deployed frontend, which
                        # detects success by scraping stdout for the legacy
                        # `claude setup-token` string and ignores this `done`
                        # event. `claude auth login` never prints that string, so
                        # without this line the dialog sits in "verifying" until
                        # its 7s timeout and then shows a (false) error. Emit the
                        # recognized line as a normal stdout event so the unchanged
                        # frontend goes green; `done` below stays the real signal
                        # for any client that reads it.
                        # Fallback only: if the success line was seen mid-stream we
                        # already emitted this (earlier = faster); don't duplicate.
                        if not compat_success_sent:
                            yield f"data: {json.dumps({'type': 'stdout', 'line': 'Authentication token created successfully'})}\n\n"
                        yield f"data: {json.dumps({'type': 'done', 'returncode': 0})}\n\n"
                    else:
                        err_msg = last_failure_line or "Login failed. Please try again."
                        print(f"[setup-token] login failed (returncode={returncode})")
                        yield f"data: {json.dumps({'type': 'error', 'error': err_msg})}\n\n"
                    break

                # Forward escape-free text so the frontend never re-encodes terminal
                # bytes into the link. Emit the authorize URL once, cleaned.
                clean_line = _strip_ansi(value)
                stripped = clean_line.strip()
                # Substring (not startswith): with PTY echo off the non-terminated
                # "Paste code here if prompted > " prompt can prefix the next line.
                if "Login failed" in stripped:
                    # Capture from the marker so a prompt prefix is dropped from the
                    # message surfaced as `error` on exit 1.
                    last_failure_line = stripped[stripped.find("Login failed"):]
                if "Invalid code" in stripped:
                    # Retryable: the CLI keeps waiting for another line, so we keep
                    # the session alive (do NOT kill/clear). The line itself also
                    # streams to the UI below as a normal stdout/stderr event.
                    #
                    # Publish it as a STRUCTURED event too. We already recognise the
                    # condition here; forwarding only the raw English line forced every
                    # client to re-parse prose emitted by a CLI none of them own, and a
                    # client that failed to match it sat on "verifying" until the
                    # session deadline. This is the same reason auth_url exists rather
                    # than each client scraping the "visit:" line.
                    #
                    # Deliberately NOT `error`: that type is terminal in the client
                    # contract, and this state is recoverable — the session is still
                    # live and another paste against the same session_id still works.
                    print("[setup-token] CLI reported invalid code (retryable; session kept alive)")
                    yield f"data: {json.dumps({'type': 'code_rejected', 'retryable': True, 'message': stripped[stripped.find('Invalid code'):]})}\n\n"
                if not compat_success_sent and "Login successful" in stripped:
                    # `claude auth login` prints "Login successful." right after the
                    # OAuth exchange, a beat BEFORE the process exits. The deployed
                    # frontend only recognizes the legacy compat string, so without
                    # this the dialog waits for the exit-0 shim below — ~100–500 ms
                    # of dead time. Emit the compat `stdout` event now; the raw line
                    # still forwards normally below, and the exit-0 branch keeps the
                    # emission as a fallback (deduped via this flag) in case a future
                    # CLI reword makes this substring miss.
                    compat_success_sent = True
                    print("[setup-token] success line observed (early compat shim)")
                    yield f"data: {json.dumps({'type': 'stdout', 'line': 'Authentication token created successfully'})}\n\n"
                if kind == "stdout":
                    auth_url = _extract_auth_url(clean_line)
                    if auth_url:
                        _setup_token_auth_url = auth_url  # cache for attach replay
                        if auth_url_sent:
                            continue  # drop duplicate redraws of the same URL
                        auth_url_sent = True
                        clean_line = auth_url
                        yield f"data: {json.dumps({'type': 'auth_url', 'url': auth_url})}\n\n"
                yield f"data: {json.dumps({'type': kind, 'line': clean_line})}\n\n"
        except FileNotFoundError:
            clear_session()
            print(f"[setup-token] CLI not found: {cli_path}")
            yield f"data: {json.dumps({'type': 'error', 'error': f'CLI not found: {cli_path}'})}\n\n"
        except Exception as e:
            clear_session()
            print(f"[setup-token] unexpected error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            # Don't kill the process or clear the session here — the process
            # must stay alive so the callback can still paste the OAuth code
            # even after the SSE client disconnects.  Cleanup happens in
            # wait_done() when the process exits naturally, or in clear_session().
            if process is not None and process.returncode is not None:
                clear_session()
            rc = process.returncode if process is not None else None
            # returncode=None here means the client disconnected while the CLI
            # was still waiting — pair this line with a subsequent "attaching
            # stream" line to see a frontend that reconnects after a drop.
            print(
                f"[setup-token] SSE generator finished (session_id={session_id}, "
                f"attach={attach}, auth_url_sent={auth_url_sent}, returncode={rc})"
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.delete("/connect/claude-code")
@router.delete("/claude/setup-token", include_in_schema=False)  # legacy alias
async def claude_setup_token_cancel():
    """
    Explicitly cancel the active setup-token session, if any (the D6 escape
    hatch for a wedged login, alongside ``force=1`` on the POST above).

    Kills the CLI process, closes the PTY master fd, and clears all session
    state. Idempotent: returns 200 even when nothing is active, so callers
    (e.g. a dialog's unmount/close handler) can call this unconditionally
    without special-casing "nothing to cancel".
    """
    global _setup_token_process, _setup_token_stdin, _setup_token_pty_master, _setup_token_session_id, _setup_token_ready
    global _setup_token_queue, _setup_token_auth_url, _setup_token_started_at

    async with _setup_token_lock:
        process = _setup_token_process
        session_id = _setup_token_session_id
        if process is None:
            print("[setup-token] cancel: no active session (idempotent no-op)")
            return {"ok": True, "message": "No active session", "session_id": None}
        if process.returncode is None:
            print(f"[setup-token] cancel: killing active session {session_id} (pid={process.pid})")
            process.kill()
        else:
            print(f"[setup-token] cancel: clearing already-finished session {session_id}")
        # Full teardown here (not the guarded clear_session() closure from
        # generate(), which is per-stream and out of scope here) — this is an
        # explicit user-initiated cancel, not a background race we need to
        # guard against, so it's safe to clear unconditionally under the lock.
        _setup_token_process = None
        _setup_token_stdin = None
        _setup_token_session_id = None
        _setup_token_ready = None
        _setup_token_queue = None
        _setup_token_auth_url = None
        _setup_token_started_at = None
        _close_setup_token_pty_master()

    return {"ok": True, "message": "Session cancelled", "session_id": session_id}
