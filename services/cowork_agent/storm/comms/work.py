"""The tools for a session's own work (design ch 12 §5.6, §5.7; ch 11 §8):
chat memory, the project's todos and sharing files.

* **Memory** is the caller's chat's (a thread reads its project's; only
  coordinators write), compare-and-set on ``if_version``.
* **Todos** go through today's todo store, the only writer of
  ``<project>/.xo/todos.json``, so the file, the timeline lines and the
  counters are exactly what today's HTTP API makes. ``runtime`` is the
  session's agent and ``session_id`` its storm id, set by the server. A plain
  session has todos only when it runs inside a project folder. No delete:
  ``cancelled`` is the outcome.
* **share_files** points the owner at files in the session's own folder
  (checked after resolving links; regular files, ≤ 50 MB each) with a
  ``share`` event, and adds them to a thread's outputs. Nothing is copied.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from pathlib import Path
from typing import Any

from services.cowork_agent.scopes import VisualizerScope
from services.cowork_agent.storm.chats import memory, rows
from services.cowork_agent.storm.comms import args as a
from services.cowork_agent.storm.comms.calls import CommsError, ToolCall, current_turn
from services.cowork_agent.storm.comms.tools import MAX_SHARED
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.places import project_of
from services.cowork_agent.visualizer.todo_status import TODO_STATUSES
from services.cowork_agent.visualizer.todos_store import TodosStoreError

SHARE_MAX_BYTES = 50 * 1024 * 1024
CAPTION_MAX = 500


# ── memory ───────────────────────────────────────────────────────────────────

async def read_memory(t: ToolCall) -> dict[str, Any]:
    path = a.text(t.args, "path", required=False, limit=100) or memory.INDEX
    chat_id = t.caller.chat_id

    def read() -> dict[str, Any]:
        out = memory.read(chat_id, path)
        if path == memory.INDEX:
            out["files"] = memory.list_files(chat_id)
        return out
    found = await asyncio.to_thread(read)
    return await t.write(lambda w: found)


async def update_memory(t: ToolCall) -> dict[str, Any]:
    path = a.text(t.args, "path", limit=100)
    if_version = a.text(t.args, "if_version", limit=memory.VERSION_HEX)
    content = t.args.get("content")
    if not isinstance(content, str):
        raise CommsError("invalid_argument", "content must be text.")
    chat_id, session_id = t.caller.chat_id, t.caller.session_id
    async with t.chats.memory_lock(chat_id):
        written = await asyncio.to_thread(memory.write, chat_id, path, content, if_version=if_version)

        def fn(w: Writes) -> dict[str, Any]:
            w.execute("INSERT INTO memory_versions (chat_id, path, version, updated_by_session, updated_at) "
                      "VALUES (?, ?, ?, ?, ?) ON CONFLICT (chat_id, path) DO UPDATE SET version = excluded.version, "
                      "updated_by_session = excluded.updated_by_session, updated_at = excluded.updated_at",
                      (chat_id, path, written["version"], session_id, w.now))
            return written
        return await t.write(fn)


# ── todos ────────────────────────────────────────────────────────────────────

async def _project(t: ToolCall) -> str:
    name = await asyncio.to_thread(lambda: project_of(t.caller.cwd).name)
    if not name:
        raise CommsError("no_project", "This session doesn't run in a project folder, so it has no todo list.")
    return name


async def _todos(call) -> Any:
    try:
        return await asyncio.to_thread(call)
    except TodosStoreError as exc:
        raise CommsError(exc.code, exc.message) from exc


async def add_todo(t: ToolCall) -> dict[str, Any]:
    content = a.text(t.args, "content", limit=1000)
    active_form = a.text(t.args, "active_form", required=False, limit=1000)
    description = a.text(t.args, "description", required=False, limit=4000)
    scope = VisualizerScope(await _project(t))
    todo = await _todos(lambda: scope.create_todo(runtime=t.caller.agent, session_id=t.caller.session_id,
                                                     content=content, active_form=active_form,
                                                     description=description))
    return await t.write(lambda w: {"todo_id": todo["id"], "status": todo["status"]})


async def update_todo(t: ToolCall) -> dict[str, Any]:
    todo_id = a.text(t.args, "todo_id", limit=64)
    changes = {"status": a.choice(t.args, "status", TODO_STATUSES),
               "content": a.text(t.args, "content", required=False, limit=1000),
               "active_form": a.text(t.args, "active_form", required=False, limit=1000)}
    scope = VisualizerScope(await _project(t))
    if await _todos(lambda: scope.get_todo(todo_id)) is None:
        raise CommsError("todo_not_found", f"There is no todo {todo_id!r} in this project.")
    todo = await _todos(lambda: scope.update_todo(todo_id, **{k: v for k, v in changes.items() if v is not None}))
    return await t.write(lambda w: {"todo_id": todo_id, "todo": todo})


async def list_todos(t: ToolCall) -> dict[str, Any]:
    status = a.choice(t.args, "status", TODO_STATUSES)
    scope = VisualizerScope(await _project(t))
    document = await _todos(scope.read_todos) or {}
    todos = [{**todo, "session_id": session_id} for session_id, entry in (document.get("sessions") or {}).items()
             for todo in entry.get("todos") or [] if not todo.get("deleted_at")
             and (status is None or todo.get("status") == status)]
    return await t.write(lambda w: {"todos": todos})


# ── sharing ──────────────────────────────────────────────────────────────────

def _shareable(cwd: str, paths: list[str]) -> list[dict[str, Any]]:
    """Each path inside the session's own folder (after resolving links), a
    regular file, not too big. Blocking."""
    root = Path(os.path.realpath(cwd))
    shared = []
    for raw in paths:
        real = Path(os.path.realpath(root / raw))
        if real != root and root not in real.parents:
            raise CommsError("outside_folder", f"{raw} isn't inside your working folder.")
        try:
            info = real.stat()
        except OSError:
            raise CommsError("no_such_file", f"There is no file {raw}.") from None
        if not real.is_file():
            raise CommsError("not_a_file", f"{raw} isn't a regular file.")
        if info.st_size > SHARE_MAX_BYTES:
            raise CommsError("too_large", f"{raw} is over {SHARE_MAX_BYTES // (1024 * 1024)} MB.")
        shared.append({"path": str(real.relative_to(root)), "name": real.name,
                       "type": mimetypes.guess_type(real.name)[0] or "application/octet-stream",
                       "bytes": info.st_size})
    return shared


async def share_files(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    paths = a.ids(t.args, "paths", limit=MAX_SHARED)
    if not paths:
        raise CommsError("missing_argument", "paths must name at least one file.")
    caption = a.text(t.args, "caption", required=False, limit=CAPTION_MAX)
    shared = await asyncio.to_thread(_shareable, caller.cwd, paths)

    def fn(w: Writes) -> dict[str, Any]:
        w.append("share", {"files": shared, "caption": caption, "folder": caller.cwd}, chat_id=caller.chat_id,
                 session_id=caller.session_id, thread_ts=caller.thread_ts,
                 turn_id=current_turn(w, caller.session_id), epoch=caller.epoch)
        if caller.thread_ts:
            thread = rows.get_thread(w, caller.chat_id, caller.thread_ts)
            known = {f["path"] for f in thread["outputs"]}
            rows.update_thread(w, caller.chat_id, caller.thread_ts,
                               outputs=[*thread["outputs"], *(f for f in shared if f["path"] not in known)])
        return {"shared": shared}
    return await t.write(fn)
