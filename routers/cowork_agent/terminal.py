"""``/api/terminal/*``: shells in the browser, kept alive and tracked.

Thin handlers over ``services/terminal``. A terminal runs as the user the
server runs as, so it can do anything that user can; the routes therefore
decide *who* may open one:

- every change (create, attach, rename, close) is a write, so it passes
  ``BrowserWriteGuard``, and must also come from a loopback peer with an
  allowed origin (``is_local_mutation``), like Space's other routes that run
  programs;
- the WebSocket is checked again on its own (``websocket_allowed``: a
  handshake is a GET, which no write guard sees), and must present an attach
  ticket from one of those writes as its first message.

Off unless ``QUIRQ_TERMINAL_ENABLED`` is set (``services/terminal/config.py``).

WebSocket protocol (``/api/terminal/sessions/{id}/ws``):

- client → server, first: ``{"type": "auth", "ticket": "…", "rows": 40, "cols": 120}``
  (within 5 s); then binary frames are keystrokes, and text frames are
  ``{"type": "resize", "rows", "cols"}``, ``{"type": "pause"}`` or ``{"type": "resume"}``.
- server → client: binary frames are terminal output. Text frames:
  ``{"type": "ready", "terminal": {…}}``; ``{"type": "resync"}`` (clear the screen:
  the next bytes start over from the kept output); ``{"type": "exit", "code",
  "reason"}`` before a normal close; ``{"type": "error", "code", "message"}``.
- close codes: 4401 bad or expired ticket, 4403 terminals off, 4404 no such
  terminal, 1008 bad first message, 1009 frame too large. A refused origin gets
  HTTP 403 before the socket opens.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import Field

from routers.browser_guard import is_local_mutation, is_loopback_peer, websocket_allowed
from routers.cowork_agent.bff.errors import ForbidExtra, http_error
from services.errors import ServiceError
from services.terminal import config
from services.terminal.sessions import TerminalError, Viewer, registry

router = APIRouter()

AUTH_TIMEOUT_SECONDS = 5.0
MAX_INPUT_FRAME = 1 << 20
MAX_CONTROL_FRAME = 4096
OUTPUT_FRAME = 64 * 1024


def _require_local(request: Request) -> None:
    if not is_local_mutation(request):
        raise HTTPException(status_code=403, detail={
            "code": "not_local",
            "message": "Terminals open only from this machine's own pages, or through a proxy on this machine.",
        })


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ServiceError as exc:
        raise http_error(exc) from exc


async def _acall(coro):
    try:
        return await coro
    except ServiceError as exc:
        raise http_error(exc) from exc


class CreateTerminal(ForbidExtra):
    project: Optional[str] = Field(default=None, max_length=255)
    path: Optional[str] = Field(default=None, max_length=1024)
    name: Optional[str] = Field(default=None, max_length=200)
    rows: Optional[int] = None
    cols: Optional[int] = None


class RenameTerminal(ForbidExtra):
    name: str = Field(max_length=200)


@router.get("/api/terminal/status")
def terminal_status() -> dict:
    reason = config.unavailable_reason()
    return {
        "enabled": reason is None,
        "reason": reason,
        "max_sessions": config.max_sessions(),
        "detached_ttl_seconds": config.detached_ttl_seconds(),
        **registry.counts(),
    }


@router.get("/api/terminal/sessions")
def list_terminals(project: Optional[str] = None, path: Optional[str] = None, workspace: bool = False) -> dict:
    """Every terminal (running, or exited within the last hour), oldest first.
    ``?project=<name>`` keeps one project's (``&path=`` one folder in it, e.g. a
    thread's worktree); ``?workspace=true`` the workspace ones."""
    return {"terminals": [t.to_dict() for t in registry.list(project=project, path=path, workspace=workspace)]}


@router.get("/api/terminal/sessions/{terminal_id}")
def get_terminal(terminal_id: str) -> dict:
    return {"terminal": _call(registry.get, terminal_id).to_dict()}


@router.post("/api/terminal/sessions", status_code=201, dependencies=[Depends(_require_local)])
async def create_terminal(body: CreateTerminal) -> dict:
    """Open a terminal in ``project``'s folder (or ``path`` inside it, e.g. a
    thread's worktree), or in the projects root without a project."""
    terminal, ticket = await _acall(registry.create(project=body.project, path=body.path, name=body.name,
                                                    rows=body.rows, cols=body.cols))
    return {"terminal": terminal.to_dict(), "ticket": ticket, "ticket_expires_in": config.TICKET_TTL_SECONDS}


@router.post("/api/terminal/sessions/{terminal_id}/attach", dependencies=[Depends(_require_local)])
def attach_ticket(terminal_id: str) -> dict:
    """A fresh ticket to (re)attach a browser to a running terminal."""
    reason = config.unavailable_reason()
    if reason:
        raise http_error(TerminalError("terminal_disabled", reason, status=403))
    return {"ticket": _call(registry.issue_ticket, terminal_id), "ticket_expires_in": config.TICKET_TTL_SECONDS}


@router.patch("/api/terminal/sessions/{terminal_id}", dependencies=[Depends(_require_local)])
def rename_terminal(terminal_id: str, body: RenameTerminal) -> dict:
    return {"terminal": _call(registry.rename, terminal_id, body.name).to_dict()}


@router.delete("/api/terminal/sessions/{terminal_id}", dependencies=[Depends(_require_local)])
async def close_terminal(terminal_id: str) -> dict:
    """Close the terminal and everything running in it, and forget it."""
    await _acall(registry.close(terminal_id))
    return {"ok": True, "closed": terminal_id}


# ── the WebSocket ────────────────────────────────────────────────────────────

async def _close(websocket: WebSocket, code: int, reason: str = "") -> None:
    try:
        await websocket.close(code=code, reason=reason[:120])
    except RuntimeError:
        pass  # already closed


async def _first_message(websocket: WebSocket) -> Optional[dict]:
    try:
        message = await asyncio.wait_for(websocket.receive(), AUTH_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return None
    text = message.get("text")
    if not isinstance(text, str) or len(text) > MAX_CONTROL_FRAME:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) and payload.get("type") == "auth" else None


async def _send_output(websocket: WebSocket, terminal, viewer: Viewer) -> None:
    """Send the terminal's output to one viewer, from its cursor, until it exits."""
    while True:
        viewer.wake.clear()
        while not viewer.paused:
            data, viewer.cursor, resynced = terminal.scrollback.read(viewer.cursor, OUTPUT_FRAME)
            if resynced:
                await websocket.send_text(json.dumps({"type": "resync"}))
            if not data:
                break
            await websocket.send_bytes(data)
        if not terminal.running and not viewer.paused and viewer.cursor >= terminal.scrollback.end:
            await websocket.send_text(json.dumps(
                {"type": "exit", "code": terminal.exit_code, "reason": terminal.ended_reason}))
            await _close(websocket, 1000, "terminal ended")
            return
        await viewer.wake.wait()


async def _error(websocket: WebSocket, code: str, message: str) -> None:
    await websocket.send_text(json.dumps({"type": "error", "code": code, "message": message}))


@router.websocket("/api/terminal/sessions/{terminal_id}/ws")
async def terminal_socket(websocket: WebSocket, terminal_id: str) -> None:
    if not (is_loopback_peer(websocket) and websocket_allowed(websocket)):
        await websocket.close(code=4403)  # before accept: the handshake gets HTTP 403
        return
    await websocket.accept()
    reason = config.unavailable_reason()
    if reason:
        await _close(websocket, 4403, reason)
        return
    first = await _first_message(websocket)
    if first is None:
        await _close(websocket, 1008, "expected an auth message first")
        return
    if not registry.consume_ticket(terminal_id, first.get("ticket")):
        await _close(websocket, 4401, "bad or expired ticket")
        return
    terminal = registry.find(terminal_id)
    if terminal is None:
        await _close(websocket, 4404, "no such terminal")
        return

    viewer = terminal.attach()
    sender: Optional[asyncio.Task] = None
    receive: Optional[asyncio.Future] = None
    try:
        await websocket.send_text(json.dumps({"type": "ready", "terminal": terminal.to_dict()}))
        if "rows" in first or "cols" in first:
            terminal.resize(first.get("rows"), first.get("cols"))
        sender = asyncio.get_running_loop().create_task(_send_output(websocket, terminal, viewer))
        while True:
            receive = asyncio.ensure_future(websocket.receive())
            done, _ = await asyncio.wait({receive, sender}, return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                receive.cancel()
                break
            message = receive.result()
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                if len(data) > MAX_INPUT_FRAME:
                    await _close(websocket, 1009, "input frame too large")
                    break
                if data and not terminal.write(data):
                    await _error(websocket, "input_refused",
                                 "The terminal is not taking input (it ended, or too much is pending).")
                continue
            await _control(websocket, terminal, viewer, message.get("text"))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        terminal.detach(viewer)
        for task in (receive, sender):
            if task is not None and not task.done():
                task.cancel()
            elif task is not None and not task.cancelled():
                task.exception()  # retrieved: a socket that went away mid-send is not an error


async def _control(websocket: WebSocket, terminal, viewer: Viewer, text) -> None:
    if not isinstance(text, str) or len(text) > MAX_CONTROL_FRAME:
        await _error(websocket, "bad_message", "Control messages are JSON text of at most 4 KiB.")
        return
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    kind = payload.get("type") if isinstance(payload, dict) else None
    if kind == "resize":
        terminal.resize(payload.get("rows"), payload.get("cols"))
    elif kind == "pause":
        viewer.paused = True
    elif kind == "resume":
        viewer.paused = False
        viewer.wake.set()
    else:
        await _error(websocket, "bad_message", "Unknown control message; expected resize, pause or resume.")
