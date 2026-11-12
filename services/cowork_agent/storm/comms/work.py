"""The tools for a session's own work (design ch 12 §5.6, §5.7; ch 11 §8;
w5-w7-design W5): chat memory, the project's todos, sharing files, the
records of its work and reading its state.

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
  Each file is described for inline display (``chats/shared.py``: its
  project address, Made item, and an image's size and tint); the session's
  next message in the turn carries them.
* **Records** (record design v2): a coordinator's ``set_goal`` records the
  founder's goal, citing their message; a thread's ``report`` lands on its
  task and ``submit_result`` records what it delivered, as reported, with
  the server's own hashes of its artifacts, and marks the task done. None
  of them wakes anyone. A thread with no task yet (it predates the upgrade,
  or a job or a continuation started it) gets one at its first report or
  result, traced to whoever started it.
* **read_state**: the Space advisory's facts and the caller's own records,
  as of now.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Mapping

from services.cowork_agent.scopes import VisualizerScope
from services.cowork_agent.storm.chats import memory, rows
from services.cowork_agent.storm.chats import shared as shared_files
from services.cowork_agent.storm.comms import args as a
from services.cowork_agent.storm.comms.calls import CommsError, ToolCall, current_turn
from services.cowork_agent.storm.comms.talk import _cited, founders_own
from services.cowork_agent.storm.comms.tools import (
    CAPTION_MAX,
    FILE_NAME_MAX,
    ID_MAX,
    LONG_TEXT_MAX,
    MAX_SHARED,
    SHARE_TITLE_MAX,
    SHORT_TEXT_MAX,
    TEXT_MAX,
    TIME_MAX,
    TITLE_MAX,
    WORD_MAX,
)
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.places import project_of
from services.cowork_agent.storm.records import FOUNDER, goals, ledger, results, tasks
from services.cowork_agent.visualizer.todo_status import TODO_STATUSES
from services.cowork_agent.visualizer.todos_store import TodosStoreError
from services.errors import ServiceError
from services.git_safe import git
from services.timestamps import parse_ts
from utils.commands import CommandSpecError, safe_arg

SHARE_MAX_BYTES = 50 * 1024 * 1024
#: What a report's and a result's caller is told: nobody was woken.
REPORTED = "Kept as your task's latest report. It wakes no one: your coordinator reads it at its next turn."
SUBMITTED = ("Recorded as you reported it; your task is done. It wakes no one: reply in your thread so the owner "
             "sees the result.")
#: How many of a chat's tasks and goals read_state lists.
TASKS_LISTED = 50
GOALS_LISTED = 20
#: Bytes read at a time when a file artifact is hashed.
HASH_CHUNK = 1024 * 1024


# ── memory ───────────────────────────────────────────────────────────────────

async def read_memory(t: ToolCall) -> dict[str, Any]:
    path = a.text(t.args, "path", required=False, limit=FILE_NAME_MAX) or memory.INDEX
    chat_id = t.caller.chat_id

    def read() -> dict[str, Any]:
        out = memory.read(chat_id, path)
        if path == memory.INDEX:
            out["files"] = memory.list_files(chat_id)
        return out
    found = await asyncio.to_thread(read)
    return await t.write(lambda w: found)


async def update_memory(t: ToolCall) -> dict[str, Any]:
    path = a.text(t.args, "path", limit=FILE_NAME_MAX)
    if_version = a.text(t.args, "if_version", limit=memory.VERSION_HEX)
    content = t.args.get("content")
    if not isinstance(content, str):
        raise CommsError("invalid_argument", "content must be text.")
    chat_id, session_id = t.caller.chat_id, t.caller.session_id
    async with t.chats.memory_lock(chat_id):
        await t.still_current()
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
    content = a.text(t.args, "content", limit=SHORT_TEXT_MAX)
    active_form = a.text(t.args, "active_form", required=False, limit=SHORT_TEXT_MAX)
    description = a.text(t.args, "description", required=False, limit=LONG_TEXT_MAX)
    scope = VisualizerScope(await _project(t))
    await t.still_current()
    todo = await _todos(lambda: scope.create_todo(runtime=t.caller.agent, session_id=t.caller.session_id,
                                                     content=content, active_form=active_form,
                                                     description=description))
    return await t.write(lambda w: {"todo_id": todo["id"], "status": todo["status"]})


async def update_todo(t: ToolCall) -> dict[str, Any]:
    todo_id = a.text(t.args, "todo_id", limit=ID_MAX)
    changes = {"status": a.choice(t.args, "status", TODO_STATUSES),
               "content": a.text(t.args, "content", required=False, limit=SHORT_TEXT_MAX),
               "active_form": a.text(t.args, "active_form", required=False, limit=SHORT_TEXT_MAX)}
    scope = VisualizerScope(await _project(t))
    if await _todos(lambda: scope.get_todo(todo_id)) is None:
        raise CommsError("todo_not_found", f"There is no todo {todo_id!r} in this project.")
    await t.still_current()
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


def _titles(args: Mapping[str, Any], count: int) -> list[str | None]:
    """``titles``: one plain-words title per path, in order (optional; an
    empty one is none)."""
    raw = args.get("titles")
    if raw is None:
        return [None] * count
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise CommsError("invalid_argument", "titles must be a list of strings, one per path.")
    if len(raw) > count:
        raise CommsError("invalid_argument", f"titles has {len(raw)} entries for {count} paths: give one per path.")
    if any(len(x) > SHARE_TITLE_MAX for x in raw):
        raise CommsError("too_long", f"A title is at most {SHARE_TITLE_MAX} characters.")
    return [" ".join(x.split()) or None for x in raw] + [None] * (count - len(raw))


async def share_files(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    paths = a.ids(t.args, "paths", limit=MAX_SHARED)
    if not paths:
        raise CommsError("missing_argument", "paths must name at least one file.")
    caption = a.text(t.args, "caption", required=False, limit=CAPTION_MAX)
    titles = _titles(t.args, len(paths))
    shared = await asyncio.to_thread(_shareable, caller.cwd, paths)
    for entry, title in zip(shared, titles):
        if title:
            entry["title"] = title
    # What the event, the outputs and the message carry: placed for inline display (chats/shared.py).
    described = await asyncio.to_thread(lambda: [shared_files.describe(caller.chat_id, caller.cwd, f)
                                                 for f in shared])

    def fn(w: Writes) -> dict[str, Any]:
        w.append("share", {"files": described, "caption": caption, "folder": caller.cwd}, chat_id=caller.chat_id,
                 session_id=caller.session_id, thread_ts=caller.thread_ts,
                 turn_id=current_turn(w, caller.session_id), epoch=caller.epoch)
        if caller.thread_ts:
            thread = rows.get_thread(w, caller.chat_id, caller.thread_ts)
            known = {f["path"] for f in thread["outputs"]}
            rows.update_thread(w, caller.chat_id, caller.thread_ts,
                               outputs=[*thread["outputs"], *(f for f in described if f["path"] not in known)])
        return {"shared": shared}
    return await t.write(fn)


# ── records ──────────────────────────────────────────────────────────────────

def _own_task(w: Writes, t: ToolCall) -> tasks.Task:
    """The caller's thread's task, made now if it has none: titled as the
    thread, its instructions the brief (else the root message), and asked as
    ``create_task`` would have it: by the founder when the root is their own
    message; else by the brief's sender; for a continuation, by its old
    thread's asker; else (a job the founder allowed) by the founder."""
    caller = t.caller
    found = tasks.of_thread(w, caller.chat_id, caller.thread_ts)
    if found is not None:
        return found
    thread = rows.get_thread(w, caller.chat_id, caller.thread_ts)
    root = rows.get_message(w, caller.thread_ts)
    brief = w.execute("SELECT from_session, note FROM hops WHERE chat_id = ? AND thread_ts = ? AND kind = 'brief' "
                      "ORDER BY seq LIMIT 1", (caller.chat_id, caller.thread_ts)).fetchone()
    earlier = (root or {}).get("extra", {}).get("continued_from")
    before = tasks.of_thread(w, caller.chat_id, earlier) if earlier else None
    if root is not None and founders_own(root):
        asked_by = FOUNDER
    else:
        asked_by = (brief[0] if brief and brief[0] else None) or (before.asked_by if before else FOUNDER)
    instructions = (brief[1] if brief else None) or (root["text"] if root else None) or thread["title"] or ""
    task, _made = tasks.create(w, chat_id=caller.chat_id, thread_ts=caller.thread_ts,
                               title=thread["title"] or f"Thread {caller.thread_ts}", instructions=instructions,
                               asked_by=asked_by, by=caller.session_id,
                               request_key=ledger.request_key("thread", caller.chat_id, caller.thread_ts),
                               goal_id=before.goal_id if before else None)
    return task


async def report(t: ToolCall) -> dict[str, Any]:
    text = a.text(t.args, "text", limit=LONG_TEXT_MAX)

    def fn(w: Writes) -> dict[str, Any]:
        task = tasks.report(w, _own_task(w, t), text=text, actor=t.caller.session_id)
        return t.record(w, {"task_id": task.id, "reported_at": task.reported_at, "note": REPORTED}, task_id=task.id)
    return await t.engine.write(fn)


def _artifact_args(t: ToolCall) -> list[dict[str, str]]:
    raw = t.args.get("artifacts") or []
    if not isinstance(raw, list) or len(raw) > MAX_SHARED:
        raise CommsError("invalid_argument", f"artifacts is a list of at most {MAX_SHARED}.")
    out = []
    for item in raw:
        if not isinstance(item, Mapping) or len(item) != 1 or set(item) - {"path", "commit"}:
            raise CommsError("invalid_argument", "Each artifact is {\"path\": a file in your folder} or "
                                                 "{\"commit\": a ref}.")
        key = next(iter(item))
        out.append({key: a.text(item, key, limit=TEXT_MAX if key == "path" else WORD_MAX)})
    return out


def _file_hash(cwd: str, raw: str) -> dict[str, Any]:
    """A file in the session's folder (after resolving links), hashed by the
    server from its bytes. Blocking."""
    root = Path(os.path.realpath(cwd))
    real = Path(os.path.realpath(root / raw))
    if real != root and root not in real.parents:
        raise CommsError("outside_folder", f"{raw} isn't inside your working folder.")
    if not real.is_file():
        raise CommsError("no_such_file", f"There is no file {raw}.")
    digest, size = hashlib.sha256(), 0
    with real.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return {"kind": "file", "path": str(real.relative_to(root)), "sha256": digest.hexdigest(), "bytes": size}


async def _commit(cwd: str, ref: str) -> dict[str, Any]:
    """A commit of the session's folder's repository, its sha read by the
    server with git."""
    try:
        arg = safe_arg(ref)
    except CommandSpecError:
        raise CommsError("invalid_argument", f"{ref!r} isn't a ref: name a commit, a branch or HEAD.") from None
    found = await git(Path(cwd), "rev-parse", "--verify", "--quiet", f"{arg}^{{commit}}")
    if not found.ok or not found.output.strip():
        raise CommsError("no_such_commit", f"{ref} isn't a commit in your working folder's repository.")
    return {"kind": "commit", "ref": ref, "sha": found.output.strip()}


async def _model(t: ToolCall) -> str:
    """The model the session runs: its own, else the catalog's default (the
    CLI's), else ``CLI_DEFAULT`` when the catalog can't name it now."""
    chosen = await t.engine.store.read(lambda conn: conn.execute(
        "SELECT json_extract(spec, '$.model') FROM sessions WHERE id = ?", (t.caller.session_id,)).fetchone()[0])
    if chosen:
        return chosen
    try:
        catalog = await t.chats.catalog(t.caller.agent, "name a result's model", bounded=True)
    except ServiceError:
        return results.CLI_DEFAULT
    return next((m.id for m in catalog.models if m.is_default), results.CLI_DEFAULT)


async def submit_result(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    summary = a.text(t.args, "summary", limit=LONG_TEXT_MAX)
    wanted = _artifact_args(t)
    artifacts = [await asyncio.to_thread(_file_hash, caller.cwd, item["path"]) if "path" in item
                 else await _commit(caller.cwd, item["commit"]) for item in wanted]
    model = await _model(t)
    key = ledger.request_key("submit_result", caller.session_id, summary, artifacts)

    def fn(w: Writes) -> dict[str, Any]:
        task = _own_task(w, t)
        cost = w.execute("SELECT sum(cost_usd) FROM turns WHERE session_id = ?", (caller.session_id,)).fetchone()[0]
        result, _made = results.create(w, task, submitted_by=caller.session_id, agent=caller.agent, model=model,
                                       summary=summary, artifacts=artifacts, cost_usd=cost, request_key=key)
        task = tasks.move(w, task, state="done", actor=caller.session_id)
        return t.record(w, {"result_id": result.id, "task_id": task.id, "task_state": task.state,
                            "artifacts": json.loads(result.artifacts), "note": SUBMITTED},
                        task_id=task.id, result_id=result.id)
    return await t.engine.write(fn)


async def set_goal(t: ToolCall) -> dict[str, Any]:
    """Record the founder's goal, citing their message (checked: theirs, in
    this chat); or, with ``goal_id``, close one as done or failed."""
    caller = t.caller
    if t.args.get("goal_id") is not None:
        return await _close_goal(t)
    stray = sorted({"state", "reason"} & set(t.args))
    if stray:
        raise CommsError("invalid_argument", f"{', '.join(stray)} close a goal, with goal_id; a new goal takes "
                                             "message_id, title and ends_at.")
    message_id = a.text(t.args, "message_id")
    title = a.text(t.args, "title", limit=TITLE_MAX)
    raw_end = a.text(t.args, "ends_at", limit=TIME_MAX)
    end = parse_ts(raw_end)
    if end is None:
        raise CommsError("invalid_argument", "ends_at is an ISO-8601 time with a UTC offset, such as "
                                             "2026-11-12T18:00:00Z.")
    if end.timestamp() <= t.engine.clock.time():
        raise CommsError("invalid_argument", "ends_at must be in the future.")
    key = ledger.request_key("set_goal", caller.session_id, message_id, title, raw_end)

    def fn(w: Writes) -> dict[str, Any]:
        [cited] = _cited(w, t, [message_id])
        goal, _made = goals.create(w, chat_id=caller.chat_id, cites=cited["id"], title=title, ends_at=raw_end,
                                   asked_by=caller.session_id, request_key=key)
        return t.record(w, _goal_view(goal), goal_id=goal.id)
    return await t.engine.write(fn)


async def _close_goal(t: ToolCall) -> dict[str, Any]:
    goal_id = a.text(t.args, "goal_id", limit=ID_MAX)
    state = a.choice(t.args, "state", ("done", "failed"), required=True)
    reason = a.text(t.args, "reason", required=False, limit=WORD_MAX)
    stray = sorted({"message_id", "title", "ends_at"} & set(t.args))
    if stray:
        raise CommsError("invalid_argument", f"Closing a goal takes goal_id, state and reason; not {', '.join(stray)}.")

    def fn(w: Writes) -> dict[str, Any]:
        goal = goals.get(w, goal_id)
        if goal is None or goal.chat_id != t.caller.chat_id:
            raise CommsError("no_such_goal", f"There is no goal {goal_id!r} in this chat.")
        if goal.state in goals.FINAL:
            raise CommsError("goal_ended", f"Goal {goal_id} is already {goal.state}.")
        moved = goals.move(w, goal, state=state, reason=reason, actor=t.caller.session_id)
        return t.record(w, _goal_view(moved), goal_id=goal.id)
    return await t.engine.write(fn)


def _goal_view(goal: goals.Goal) -> dict[str, Any]:
    return {"goal_id": goal.id, "title": goal.title, "ends_at": goal.ends_at, "state": goal.state,
            "state_reason": goal.state_reason, "cites": goal.cites, "asked_by": goal.asked_by}


def _task_view(w: Writes, task: tasks.Task) -> dict[str, Any]:
    done = results.of_task(w, task.id)
    latest = done[-1] if done else None
    return {"task_id": task.id, "thread_id": task.thread_ts, "title": task.title, "state": task.state,
            "goal_id": task.goal_id, "asked_by": task.asked_by, "last_report": task.last_report,
            "reported_at": task.reported_at, "results": len(done),
            "latest_result": {"result_id": latest.id, "summary": latest.summary, "at": latest.created_at,
                              "artifacts": json.loads(latest.artifacts)} if latest else None}


def _open_questions(w: Writes, session_id: str) -> list[dict[str, Any]]:
    """The caller's own asks still waiting: to the founder (open items it
    raised) and to a coordinator (asks not yet answered)."""
    items = [{"to": "founder", "review_id": r[0], "kind": r[1], "title": r[2], "blocking": bool(r[3]),
              "asked_at": r[4]} for r in w.execute(
        "SELECT id, kind, title, blocking, created_at FROM review_items WHERE session_id = ? AND origin = 'agent' "
        "AND state = 'open' ORDER BY seq", (session_id,))]
    hops = [{"to": "workspace" if r[1] == "workspace-coordinator" else "coordinator", "hop_id": r[0],
             "asked_at": r[2]} for r in w.execute(
        "SELECT id, to_role, created_at FROM hops WHERE from_session = ? AND kind = 'ask' AND state <> 'answered' "
        "ORDER BY seq", (session_id,))]
    return items + hops


async def read_state(t: ToolCall) -> dict[str, Any]:
    """The advisory's snapshot, and the caller's goals, tasks (with their
    latest reports and results) and open questions; a coordinator also gets
    its chat's items waiting on the owner."""
    caller = t.caller
    advisory = t.engine.advisory
    snapshot = advisory.snapshot().as_dict(advisory.now())

    def fn(w: Writes) -> dict[str, Any]:
        if caller.role == "thread":
            own = tasks.of_thread(w, caller.chat_id, caller.thread_ts)
            listed = [own] if own else []
            chat_goals = [g for g in (goals.get(w, own.goal_id),) if g] if own and own.goal_id else []
        elif caller.chat_id:
            listed = tasks.of_chat(w, caller.chat_id, limit=TASKS_LISTED)
            chat_goals = goals.of_chat(w, caller.chat_id)[:GOALS_LISTED]
        else:
            listed, chat_goals = [], []
        out = {"advisory": snapshot, "goals": [_goal_view(g) for g in chat_goals],
               "tasks": [_task_view(w, task) for task in listed],
               "open_questions": _open_questions(w, caller.session_id)}
        if caller.role.endswith("coordinator"):
            out["open_items"] = [dict(zip(("review_id", "thread_id", "kind", "risk", "blocking", "title",
                                           "created_at"), r)) for r in w.execute(
                "SELECT id, thread_ts, kind, risk, blocking, title, created_at FROM review_items WHERE chat_id = ? "
                "AND state = 'open' ORDER BY urgency, seq", (caller.chat_id,))]
        return out
    return await t.write(fn)
