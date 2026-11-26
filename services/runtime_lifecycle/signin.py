"""Signing a runtime in through its own CLI, on a pseudo-terminal, streamed as
SSE frames: ``session``, ``auth_url``, ``stdout``, then ``done`` or ``error``
(the shape the old Connect dialogs read, so one component serves every runtime).
The flow yields events (``session``, ``auth_url``, ``line``, ``expired``,
``signed_in``, ``failed``, ``error``, ``heartbeat``) that a view renders as
frames: ``views.standard``, or a runtime's ``Legacy.view`` on its old URLs.

One sign-in per runtime at a time. A second stream while one runs attaches to
it: same session, same CLI, the link replayed; the older stream ends with an
error saying it was superseded, and a code pasted against the session id
still works. A stream that disconnects stops its CLI. The local login check is
polled while the CLI runs, so success is seen the moment its file lands.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.cowork_agent.registry.agent_registry import installed
from services.runtime_lifecycle import events
from services.runtime_lifecycle.contract import LifecycleError, View
from services.runtime_lifecycle.views import EXPIRED_LINE, SUCCESS_LINE, error_event, standard  # noqa: F401
from services.runtime_lifecycle.registry import Runtime
from utils.commands import PtyProcess, spawn_pty

TIMEOUT_S = 900.0           # a device code lasts 15 minutes; a paste has the person in the loop
HEARTBEAT_S = 15.0
READY_TIMEOUT_S = 30.0
POLL_S = 1.0
SUPERSEDED = "This connection was superseded by a newer one; the login session is still active."
CANCELLED = "This login session was cancelled. Start a new one to get a fresh sign-in link."

_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[\x20-\x2f]+[\x30-\x7e]"
                   r"|\x1b[@-Z\\-_]")


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


class CodeBody(BaseModel):
    code: str           # the code pasted from the browser
    session_id: str     # the SSE stream's session id


def sse(frames: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(frames, media_type="text/event-stream", headers=SSE_HEADERS)


def strip_ansi(line: str) -> str:
    return _ANSI.sub("", line).replace("\x07", "")


def frame(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def error_frame(message: str, **extra) -> str:
    return frame(error_event(message, **extra))


@dataclass(eq=False)
class _Session:
    id: str
    queue: asyncio.Queue
    started: float = field(default_factory=time.monotonic)
    pty: PtyProcess | None = None
    auth_url: str | None = None
    code_sent: bool = False
    ready: asyncio.Event = field(default_factory=asyncio.Event)


_live: dict[str, _Session] = {}
_finished: dict[str, tuple[str, bool]] = {}     # runtime -> (session id, ok) of its last sign-in
_reaping: set[asyncio.Task] = set()


def _background(work) -> None:
    task = asyncio.get_running_loop().create_task(work)
    _reaping.add(task)
    task.add_done_callback(_reaping.discard)


def _stop(pty: PtyProcess | None) -> None:
    """Hang the CLI up now; its reaping finishes in the background."""
    if pty is not None and pty.returncode is None:
        with contextlib.suppress(OSError):
            os.killpg(pty.pid, signal.SIGHUP)
        _background(pty.terminate(grace=1.0))


def _end(rt: Runtime, session: _Session, ok: bool) -> None:
    if _live.get(rt.name) is session:
        del _live[rt.name]
        _finished[rt.name] = (session.id, ok)
        session.queue.put_nowait(("wake", None))     # its stream says so now, not at the next heartbeat
    _stop(session.pty)


async def _spawn(rt: Runtime, session: _Session, binary: str) -> None:
    sign_in = rt.spec.sign_in
    pending = bytearray()

    def deliver(item: tuple) -> None:
        if _live.get(rt.name) is session:
            session.queue.put_nowait(item)

    def output(data: bytes) -> None:
        session.ready.set()
        pending.extend(data)
        while (cut := pending.find(b"\n")) >= 0:
            deliver(("stdout", pending[:cut].decode("utf-8", errors="replace").rstrip("\r")))
            del pending[:cut + 1]

    def exited(code: int | None) -> None:
        if pending:
            deliver(("stdout", pending.decode("utf-8", errors="replace").rstrip("\r")))
            pending.clear()
        if session.pty is pty:
            deliver(("exit", code))

    env = {"TERM": "xterm-256color", **os.environ, **sign_in.env, "BROWSER": "true", "DISPLAY": ""}
    for name in sign_in.unset:
        env.pop(name, None)
    pty = await spawn_pty([binary, *sign_in.argv], cwd=Path.home(), env=env, cols=1000, echo=False,
                          on_output=output, on_exit=exited)
    session.pty = pty
    if sign_in.relogin:
        return      # its login is there already: only its exit says the new one landed

    async def watch() -> None:
        while pty.returncode is None:
            await asyncio.sleep(POLL_S)
            if pty.returncode is None and await asyncio.to_thread(_landed(rt)):
                deliver(("login_ok", 0))
                _stop(pty)
                return

    _background(watch())


def _landed(rt: Runtime) -> Callable[[], bool]:
    """The check a sign-in succeeds on: the CLI's own login, never a key variable."""
    return rt.spec.sign_in.landed or rt.spec.logged_in


async def _signed_in(rt: Runtime) -> None:
    """The login landed: finish it, then tell the listeners (with the credential, on a sign-in to an account)."""
    await asyncio.to_thread(rt.spec.sign_in.finish)
    credential = await asyncio.to_thread(rt.spec.credential) if rt.account_kind else None
    await events.changed(rt.name, signed_in=rt.account_kind, credential=credential)


async def stream(rt: Runtime, *, install_path: str | None = None, view: View | None = None) -> AsyncIterator[str]:
    """Sign ``rt`` in, as SSE frames (``view`` renders its events; ``standard`` by default)."""
    async with contextlib.aclosing(_events(rt, install_path)) as flow, \
            contextlib.aclosing((view or standard)(flow)) as frames:
        async for event in frames:
            yield ": heartbeat\n\n" if event["type"] == "heartbeat" else frame(event)


async def _events(rt: Runtime, install_path: str | None) -> AsyncIterator[dict]:
    """The sign-in's events; already signed in is ``signed_in`` at once, unless the CLI signs in again."""
    if rt.spec.sign_in is None:
        yield error_event(f"{rt.spec.title} signs in from a terminal only.", code="unsupported")
        return
    if not rt.spec.sign_in.relogin and await asyncio.to_thread(_landed(rt)):
        yield {"type": "session", "session_id": str(uuid.uuid4())}
        yield {"type": "signed_in", "already": True}
        return
    binary = rt.spec.binary()
    if not installed(binary):
        extra = {"install": install_path} if install_path and rt.command("install") else {}
        yield error_event(f"{rt.package} CLI not found: {binary}", code="not_installed", **extra)
        return

    queue: asyncio.Queue = asyncio.Queue()
    session = _live.get(rt.name)
    url_sent = False
    if session is not None:
        old, session.queue = session.queue, queue
        while not old.empty():
            queue.put_nowait(old.get_nowait())
        old.put_nowait(("wake", None))
    else:
        session = _live[rt.name] = _Session(str(uuid.uuid4()), queue)
        try:
            await _spawn(rt, session, binary)
        except Exception as exc:  # noqa: BLE001 - said to the person; nothing was started
            _end(rt, session, False)
            yield error_event(str(exc))
            return

    def owner() -> bool:
        return _live.get(rt.name) is session and session.queue is queue

    try:
        yield {"type": "session", "session_id": session.id}
        if session.auth_url:
            url_sent = True
            yield {"type": "auth_url", "url": session.auth_url}
        deadline = session.started + TIMEOUT_S
        while True:
            if not owner():
                yield error_event(SUPERSEDED if _live.get(rt.name) is session else CANCELLED)
                return
            try:
                kind, value = await asyncio.wait_for(queue.get(), HEARTBEAT_S)
            except asyncio.TimeoutError:
                if owner() and time.monotonic() >= deadline:
                    _end(rt, session, False)
                    yield error_event(f"{rt.spec.title} login timed out", code="timeout")
                    return
                yield {"type": "heartbeat"}
                continue
            if kind == "wake":
                continue
            if kind == "stdout":
                line = strip_ansi(value)
                link = rt.spec.sign_in.auth_url(line)
                url = None if url_sent else link
                if url:
                    session.auth_url, url_sent = url, True
                yield {"type": "line", "line": line, "auth_url": url, "link": bool(link)}
                continue
            # Only the local login check is success; an exit code alone isn't.
            if kind == "login_ok" or ((value == 0 or not rt.spec.sign_in.relogin)
                                      and await asyncio.to_thread(_landed(rt))):
                _end(rt, session, True)
                _background(_signed_in(rt))     # never lost to a client that hangs up on ``done``
                yield {"type": "signed_in", "already": False}
                return
            if (rt.spec.sign_in.style == "paste_code" and url_sent and not session.code_sent
                    and time.monotonic() < deadline):
                # The CLI gave up before a code came: a fresh link, not a failure.
                url_sent, session.auth_url = False, None
                await _spawn(rt, session, binary)
                yield {"type": "expired"}
                continue
            _end(rt, session, False)
            yield {"type": "failed", "returncode": value, "message": f"{rt.spec.title} login failed. Please try again."}
            return
    finally:
        if owner():
            _end(rt, session, False)


async def submit(rt: Runtime, session_id: str, code: str, *, start_path: str, sent: str = "Code sent") -> dict:
    """Paste ``code`` into the running sign-in ``session_id``."""
    session = _live.get(rt.name)
    if session is None:
        if _finished.get(rt.name) == (session_id, True):
            return {"ok": True, "message": "Login already completed", "session_id": session_id}
        raise LifecycleError("no_session", f"No {rt.name} login session active. Start one with POST {start_path} "
                             "first.", 409)
    if rt.spec.sign_in.style != "paste_code":
        raise LifecycleError("no_code", f"{rt.spec.title} takes no pasted code.", 409)
    if session.id != session_id:
        raise LifecycleError("session_mismatch", f"Session mismatch (active '{session.id}', got '{session_id}'). "
                             "Start a new session.", 409)
    try:
        await asyncio.wait_for(session.ready.wait(), READY_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise LifecycleError("not_ready", "Login process did not become ready in time.", 504) from None
    if _live.get(rt.name) is not session or session.pty is None:
        raise LifecycleError("no_session", "Login session is no longer active.", 409)
    if session.pty.returncode is not None:
        raise LifecycleError("finished", "Login process already finished. Start a new session.", 409)
    if not session.pty.write(rt.spec.sign_in.code(code).encode("utf-8") + b"\r"):
        raise LifecycleError("input_closed", "Login process input closed.", 410)
    session.code_sent = True
    return {"ok": True, "message": sent, "session_id": session.id}


def cancel(rt: Runtime) -> dict:
    """Stop the running sign-in, if any (idempotent)."""
    session = _live.get(rt.name)
    if session is None:
        return {"ok": True, "message": "No active session", "session_id": None}
    _end(rt, session, False)
    return {"ok": True, "message": "Session cancelled", "session_id": session.id}
