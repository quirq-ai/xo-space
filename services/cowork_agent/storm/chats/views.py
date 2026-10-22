"""What the ``/api/chat/chats*`` routes show and change (design ch 17 §2.2;
ch 10 §3.3; ch 11 §3.1, §7.4, §8, §16.2). The handlers stay thin; this is
where a chat's settings, a thread's view and the person's changes are made.

* A chat shows its router override and its pin ``{agent, model, effort}``
  for its coordinator. A new pin is checked against the runtime's catalog;
  a model or effort reaches a live coordinator from its next turn, and
  another agent recycles the coordinator at its next boundary.
* A thread shows its state, card, worktree, measured agent time and live
  runs. The person resolves and reopens it (idempotent, never starting or
  stopping a turn), resumes it, renames and labels it, and pins its model
  and effort; its agent is fixed once it starts (``agent_locked``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from services.cowork_agent.storm.chats import Chats, ChatRefused, check_intelligence, memory, projects, router, rows
from services.cowork_agent.storm.chats import live_coordinator, thread_scope
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.presets import Intelligence
from services.cowork_agent.storm.runs.operations import counts as run_counts

PERSON = "person"
UNSET = object()


def _pin(conn, scope: str, scope_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT agent, model, effort, set_by, set_at FROM intelligence WHERE scope = ? AND scope_id = ?",
                       (scope, scope_id)).fetchone()
    return dict(zip(("agent", "model", "effort", "set_by", "set_at"), row)) if row else {}


def _set_pin(w: Writes, scope: str, scope_id: str, **fields: Any) -> None:
    """Upsert the conversation's ``intelligence`` row with ``fields``; the
    person set it."""
    w.execute("INSERT INTO intelligence (scope, scope_id, set_by, set_at) VALUES (?, ?, ?, ?) "
              "ON CONFLICT (scope, scope_id) DO UPDATE SET set_by = excluded.set_by, set_at = excluded.set_at",
              (scope, scope_id, PERSON, w.now))
    for column, value in fields.items():
        w.execute(f"UPDATE intelligence SET {column} = ? WHERE scope = ? AND scope_id = ?",
                  (json.dumps(value) if column == "routing" and value is not None else value, scope, scope_id))


# ── chats ────────────────────────────────────────────────────────────────────

def _chat(conn, chat: Mapping[str, Any]) -> dict[str, Any]:
    pin = _pin(conn, "chat", chat["id"])
    return {**chat, "routing": router.chat_override(conn, chat["id"]),
            "pin": {k: pin.get(k) for k in ("agent", "model", "effort")}}


async def list_chats(chats: Chats) -> list[dict[str, Any]]:
    project_chats = await projects.sync(chats)
    workspace = await chats.workspace()
    return await chats.engine.store.read(lambda conn: [_chat(conn, c) for c in (workspace, *project_chats)])


async def get_chat(chats: Chats, chat_id: str) -> dict[str, Any]:
    chat = await chats.chat(chat_id)
    return await chats.engine.store.read(lambda conn: _chat(conn, chat))


async def update_chat(chats: Chats, chat_id: str, *, routing: Any = UNSET, agent: Any = UNSET, model: Any = UNSET,
                      effort: Any = UNSET) -> dict[str, Any]:
    chat = await chats.chat(chat_id)
    chats.writable(chat)
    engine = chats.engine
    pin = await engine.store.read(lambda conn: _pin(conn, "chat", chat_id))
    wanted = {k: v for k, v in (("agent", agent), ("model", model), ("effort", effort)) if v is not UNSET}
    if wanted:
        target = wanted.get("agent", pin.get("agent"))
        _agent, runtime = await chats.runtime_for(target, "run this chat's coordinator")
        await check_intelligence(runtime, Intelligence(wanted.get("model", pin.get("model")),
                                                       wanted.get("effort", pin.get("effort"))))

    def save(w: Writes) -> None:
        if routing is not UNSET:
            if routing is not None:
                router.check(w, routing["mode"], routing.get("decision_model"))
            _set_pin(w, "chat", chat_id, routing=routing)
        if wanted:
            _set_pin(w, "chat", chat_id, **wanted)
        w.append("chat", {"id": chat_id, **({"routing": routing} if routing is not UNSET else {}), **wanted},
                 chat_id=chat_id)
    await engine.write(save)
    if wanted:
        await _apply_pin(chats, chat, wanted)
    return await get_chat(chats, chat_id)


async def _apply_pin(chats: Chats, chat: Mapping[str, Any], wanted: Mapping[str, Any]) -> None:
    """A model or effort reaches the live coordinator from its next turn;
    another agent recycles it at its next boundary (ch 10 §3.3)."""
    coordinator = await chats.engine.store.read(lambda conn: live_coordinator(conn, chat["id"]))
    if coordinator is None:
        return
    session = await chats.engine.get_session(coordinator)
    if wanted.get("agent") and wanted["agent"] != session["agent"]:
        await chats.recycle_if_due(chat)
        return
    changes = {k: wanted[k] for k in ("model", "effort") if wanted.get(k)}
    if changes:
        await chats.engine.update(coordinator, **changes)


# ── messages, hops, memory ───────────────────────────────────────────────────

async def messages(chats: Chats, chat_id: str, *, thread: str | None, after_seq: int, before_seq: int | None,
                   limit: int) -> list[dict[str, Any]]:
    await chats.chat(chat_id)
    return await chats.engine.store.read(lambda conn: rows.page_messages(
        conn, chat_id=chat_id, thread_ts=thread, timeline=thread is None, after_seq=after_seq, before_seq=before_seq,
        limit=limit))


async def hops(chats: Chats, chat_id: str, *, thread: str | None, after_seq: int, before_seq: int | None,
               limit: int) -> list[dict[str, Any]]:
    """A page of the chat's hops by seq, oldest first: the ``limit`` after
    ``after_seq``, or, with ``before_seq`` alone, the ``limit`` right before it.
    A chat's hops are those stored on it and those its own sessions sent or
    received (the workspace's forward is stored on the project it reached)."""
    await chats.chat(chat_id)
    backwards = before_seq is not None and not after_seq

    def page(conn) -> list[dict[str, Any]]:
        found = conn.execute(
            f"SELECT {rows.HOP_COLUMNS} FROM hops WHERE (chat_id = :chat OR from_session IN (SELECT id FROM sessions "
            "WHERE chat_id = :chat) OR to_session IN (SELECT id FROM sessions WHERE chat_id = :chat)) "
            "AND (:thread IS NULL OR thread_ts = :thread) AND seq > :after AND (:before IS NULL OR seq < :before) "
            f"ORDER BY seq {'DESC' if backwards else 'ASC'} "
            "LIMIT :limit", {"chat": chat_id, "thread": thread, "after": after_seq, "before": before_seq,
                             "limit": limit}).fetchall()
        return [rows.hop_view(r) for r in (reversed(found) if backwards else found)]
    return await chats.engine.store.read(page)


async def memory_files(chats: Chats, chat_id: str) -> list[dict[str, Any]]:
    await chats.chat(chat_id)
    return await asyncio.to_thread(memory.list_files, chat_id)


async def memory_file(chats: Chats, chat_id: str, path: str) -> dict[str, Any]:
    await chats.chat(chat_id)
    found = await asyncio.to_thread(memory.read, chat_id, path)
    if found["version"] == memory.NEW:
        raise ChatRefused("memory_not_found", f"This chat has no memory file {path!r}.", status=404)
    return found


# ── threads ──────────────────────────────────────────────────────────────────

def _threads(conn, chat_id: str, ts: str | None) -> list[dict[str, Any]]:
    found = [rows.get_thread(conn, chat_id, ts)] if ts else rows.list_threads(conn, chat_id)
    found = [t for t in found if t is not None]
    sessions = [t["session_id"] for t in found]
    marks = ", ".join("?" * len(sessions)) or "NULL"
    agent_ms = dict(conn.execute(f"SELECT session_id, coalesce(sum(wall_ms), 0) FROM turns WHERE session_id IN "
                                 f"({marks}) GROUP BY session_id", sessions).fetchall())
    return [{**t, "card": rows.latest_card(conn, chat_id, t["ts"]), "agent_ms": agent_ms.get(t["session_id"], 0),
             "pin": {k: v for k, v in _pin(conn, "thread", thread_scope(chat_id, t["ts"])).items()
                     if k in ("agent", "model", "effort")}} for t in found]


async def list_threads(chats: Chats, chat_id: str, ts: str | None = None) -> list[dict[str, Any]]:
    await chats.chat(chat_id)
    found = await chats.engine.store.read(lambda conn: _threads(conn, chat_id, ts))
    live = await chats.engine.get_sessions(t["session_id"] for t in found)
    runs = await run_counts(chats.engine, [t["session_id"] for t in found])
    return [{**t, "runs": runs[t["session_id"]],
             "session": {k: live[t["session_id"]][k] for k in ("state", "agent", "model", "effort", "live_runs",
                                                                "paused")}
             if t["session_id"] in live else None} for t in found]


async def get_thread(chats: Chats, chat_id: str, ts: str) -> dict[str, Any]:
    found = await list_threads(chats, chat_id, ts)
    if not found:
        raise ChatRefused("thread_not_found", f"There is no thread {ts!r} in this chat.", status=404)
    return found[0]


async def update_thread(chats: Chats, chat_id: str, ts: str, *, resolved: bool | None = None,
                        paused: bool | None = None, title: str | None = None, label: Any = UNSET,
                        intelligence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    chats.writable(await chats.chat(chat_id))
    thread = await get_thread(chats, chat_id, ts)
    engine, session_id = chats.engine, thread["session_id"]
    if paused is True:
        raise ChatRefused("bad_change", "XO pauses a thread itself (the loop breaker); send paused: false to resume "
                                        "it.")
    pin: dict[str, Any] = {}
    if intelligence:
        agent = intelligence.get("agent")
        if agent and agent != (thread["session"] or {}).get("agent"):
            raise ChatRefused("agent_locked", "A thread's agent is fixed once it starts: its history is that agent's "
                                              "own transcript. Change its model or effort, or start a new thread.",
                              status=409)
        pin = {k: intelligence[k] for k in ("model", "effort") if intelligence.get(k)}

    def save(w: Writes) -> None:
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = title
        if label is not UNSET:
            changes["label"] = label
        current = rows.get_thread(w, chat_id, ts)["state"]
        if resolved is True and current != "resolved":
            changes.update(state="resolved", resolved_at=w.now)
        elif resolved is False and current == "resolved":
            changes.update(state="working", resolved_at=None)
        if paused is False and current == "paused" and resolved is not True:
            changes["state"] = "working"
        if changes:
            rows.update_thread(w, chat_id, ts, **changes)
        if pin:
            _set_pin(w, "thread", thread_scope(chat_id, ts), **pin)
    if pin:
        await engine.update(session_id, **pin)          # checked against the catalog; from the next turn
    await engine.write(save)
    if paused is False:
        await engine.resume(session_id)
    return await get_thread(chats, chat_id, ts)
