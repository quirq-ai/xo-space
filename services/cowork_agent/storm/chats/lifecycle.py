"""What the conversation does when its sessions move (design ch 11 §5, §6.2,
§7.3, §16.2): an engine ``Listener`` that hears the store's commits and acts
on them in tasks it owns, one session at a time.

* **An input ends** (the session goes idle, ``session {status: warm}``):
  - **the reply gate** (§5): visible output is a comms call. Over every call
    since the input (all its turns: claude may give two for one input), a
    terminal or routing call, a blocking ``ask_owner`` or a
    ``propose_project`` answers it. None, and no turn failed or was stopped:
    the gate text goes in once; none after the gate: ``turn.unanswered``.
    Plain chat has no gate;
  - a thread's **card** and state (rule-based, ``cards.py``) and, when it
    replied or failed, a **status** hop with a ``later`` wake to its project
    coordinator;
  - a coordinator's **routing decision** is recorded (``router_decisions``,
    the agent as provider), and it is **recycled** if due.
* **A turn starts:** the hops whose wakes it took are ``delivered``.
* **A session is paused or resumed** (the loop breaker, the person): its
  thread's state follows.

No polling: everything here is driven by the commits.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping, Sequence

from services.cowork_agent.storm.chats import cards, envelope, router, rows
from services.cowork_agent.storm.chats import live_coordinator
from services.cowork_agent.storm.comms import tools
from services.cowork_agent.storm.engine import Wake, Writes
from services.cowork_agent.storm.store import StoredEvent

log = logging.getLogger(__name__)

GATE_REASON = "reply-gate"
GATE_TEXT = ("[xo-reply-gate] Your last turn ended without a terminal xo_comms tool call, so nothing reached anyone: "
             "plain text is not delivered. Call the right xo_comms tool now ({tools}), or no_reply_needed if no "
             "reply is warranted.")
#: Turn ends that mean the input was not the agent's to answer.
NOT_ANSWERABLE = ("stopped", "interrupted")
GATED_ROLES = ("workspace-coordinator", "project-coordinator", "thread")


class Lifecycle:
    def __init__(self, chats: Any) -> None:
        self.chats = chats
        self._locks: dict[str, asyncio.Lock] = {}

    # ── the engine's Listener ────────────────────────────────────────────

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            if event.session_id is None:
                continue
            if event.type == "session" and event.data.get("status") == "warm":
                self._spawn(self._input_ended(event.session_id, event.seq), event)
            elif event.type == "session" and event.data.get("reason") in ("paused", "resumed"):
                self._spawn(self._paused(event.session_id, event.data["reason"] == "paused"), event)
            elif event.type == "turn.started":
                self._spawn(self._delivered(event.session_id, event.turn_id), event)

    def ephemeral(self, event: Any, epoch: int) -> None:
        """Nothing here acts on what the store doesn't keep."""

    def _spawn(self, work, event: StoredEvent) -> None:
        async def serial() -> None:
            async with self._locks.setdefault(event.session_id, asyncio.Lock()):
                await work
        self.chats.spawn(serial(), f"{event.type}:{event.seq}")

    # ── an input ended ───────────────────────────────────────────────────

    async def _input_ended(self, session_id: str, seq: int) -> None:
        engine = self.chats.engine
        found = await engine.store.read(lambda conn: _input(conn, session_id, seq))
        if found is None:
            return
        await self._delivered(session_id, None, turns=[t["id"] for t in found["turns"]])
        role = found["role"]
        statuses = {t["status"] for t in found["turns"]}
        failed, stopped = "failed" in statuses, bool(statuses & set(NOT_ANSWERABLE))
        last = found["turns"][-1]["id"]
        if role in GATED_ROLES and not found["answered"] and not failed and not stopped:
            if GATE_REASON not in found["reasons"]:
                await self._gate(found, last)
                return
            await engine.write(lambda w: w.append(
                "turn.unanswered", {"turns": [t["id"] for t in found["turns"]]}, session_id=session_id,
                chat_id=found["chat_id"], thread_ts=found["thread_ts"], turn_id=last))
            failed = True
        if role == "thread":
            await self._thread_input(found, failed=failed, turn_id=last)
        elif role in ("workspace-coordinator", "project-coordinator") and not found["retired"]:
            await self._coordinator_input(found)

    async def _gate(self, found: Mapping[str, Any], turn_id: str) -> None:
        text = GATE_TEXT.format(tools=", ".join(tools.terminal_tools(found["role"])))
        await self.chats.engine.write(lambda w: w.wake(found["session_id"], Wake(
            id=f"gate:{turn_id}", text=text, priority="now", reason=GATE_REASON, origin="system")))

    async def _thread_input(self, found: Mapping[str, Any], *, failed: bool, turn_id: str) -> None:
        chat_id, ts = found["chat_id"], found["thread_ts"]
        error = found["errors"][-1] if found["errors"] else None

        def apply(w: Writes) -> None:
            thread = rows.get_thread(w, chat_id, ts)
            if thread is None:
                return
            items = w.execute("SELECT title, blocking FROM review_items WHERE chat_id = ? AND thread_ts = ? "
                              "AND state = 'open' ORDER BY seq", (chat_id, ts)).fetchall()
            card = cards.card(thread=thread, replies=found["replies"], failed=failed, error=error,
                              oldest_open_item=items[0][0] if items else None)
            seq = w.append("card", {"ts": ts, "card": card}, chat_id=chat_id, thread_ts=ts,
                           session_id=found["session_id"], turn_id=turn_id)
            w.execute("INSERT INTO cards (chat_id, thread_ts, seq, card, created_at) VALUES (?, ?, ?, json(?), ?)",
                      (chat_id, ts, seq, json.dumps(card), w.now))
            rows.update_thread(w, chat_id, ts, state=cards.thread_state(thread["state"], card["state"],
                                                                        blocked=any(b for _t, b in items)))
            if found["replies"] or card["state"] == "failed":
                _status(w, found, thread, card, turn_id)
        await self.chats.engine.write(apply)

    async def _coordinator_input(self, found: Mapping[str, Any]) -> None:
        chats = self.chats
        actions = [{"kind": router.ACTION_OF[c["tool"]], **{k: c[k] for k in ("thread_id", "hop_id", "review_id",
                                                                             "message_id") if c.get(k)}}
                   for c in found["calls"] if c.get("ok") and c.get("tool") in router.ACTION_OF]
        if found["messages"]:
            await chats.engine.write(lambda w: [router.record(
                w, chat_id=found["chat_id"], message_id=m, decided_by="agent",
                route={"provider": "agent", "actions": actions}) for m in found["messages"]])
        chat = await chats.chat(found["chat_id"])
        await chats.recycle_if_due(chat)

    # ── a turn started, a session paused ─────────────────────────────────

    async def _delivered(self, session_id: str, turn_id: str | None, *, turns: Sequence[str] = ()) -> None:
        """The hops whose wakes a turn took are delivered (ch 11 §16.2)."""
        taken = [turn_id] if turn_id else list(turns)
        if not taken:
            return

        def mark(w: Writes) -> None:
            marks = ", ".join("?" * len(taken))
            for (hop_id,) in w.execute(
                    f"SELECT h.id FROM hops h JOIN wakes k ON k.id = h.wake_id WHERE k.turn_id IN ({marks}) "
                    "AND h.state = 'sent'", taken).fetchall():
                rows.set_hop_state(w, hop_id, "delivered")
        await self.chats.engine.write(mark)

    async def _paused(self, session_id: str, paused: bool) -> None:
        def apply(w: Writes) -> None:
            row = w.execute("SELECT chat_id, thread_ts FROM sessions WHERE id = ? AND role = 'thread'",
                            (session_id,)).fetchone()
            thread = rows.get_thread(w, row[0], row[1]) if row else None
            if thread is None:
                return
            if paused and thread["state"] != "resolved":
                rows.update_thread(w, thread["chat_id"], thread["ts"], state="paused")
            elif not paused and thread["state"] == "paused":
                rows.update_thread(w, thread["chat_id"], thread["ts"], state="working")
        await self.chats.engine.write(apply)


def _input(conn, session_id: str, seq: int) -> dict[str, Any] | None:
    """What the session's input that just ended did: the turns that ended
    since it was last idle, their comms calls, the wakes they took, the
    thread's replies. None when nothing ended (it was only starting)."""
    row = conn.execute("SELECT role, chat_id, thread_ts, retired_at IS NOT NULL FROM sessions WHERE id = ?",
                       (session_id,)).fetchone()
    if row is None:
        return None
    role, chat_id, thread_ts, retired = row
    since = conn.execute("SELECT coalesce(max(seq), 0) FROM events WHERE session_id = ? AND type = 'session' "
                         "AND json_extract(data, '$.status') = 'warm' AND seq < ?", (session_id, seq)).fetchone()[0]
    window = (session_id, since, seq)
    turns = [{"id": t, "status": s, "error": e} for t, s, e in conn.execute(
        "SELECT turn_id, json_extract(data, '$.status'), json_extract(data, '$.error') FROM events "
        "WHERE session_id = ? AND type = 'turn.finished' AND seq > ? AND seq <= ? ORDER BY seq", window)]
    if not turns:
        return None
    calls = [json.loads(d) for (d,) in conn.execute(
        "SELECT data FROM events WHERE session_id = ? AND type = 'comms.call' AND seq > ? AND seq <= ? ORDER BY seq",
        window)]
    marks = ", ".join("?" * len(turns))
    taken = conn.execute(f"SELECT id, reason FROM wakes WHERE turn_id IN ({marks})", [t["id"] for t in turns]).fetchall()
    replies = [text for (text,) in conn.execute(
        "SELECT text FROM messages WHERE author_session_id = ? AND author_kind = 'agent' AND thread_ts IS ? "
        "AND seq > ? AND seq <= ? ORDER BY seq", (session_id, thread_ts, since, seq))] if role == "thread" else []
    return {"session_id": session_id, "role": role, "chat_id": chat_id, "thread_ts": thread_ts, "retired": retired,
            "turns": turns, "calls": calls, "reasons": {r for _w, r in taken}, "replies": replies,
            "messages": [w.removeprefix("msg:") for w, _r in taken if w.startswith("msg:")],
            "errors": [t["error"] for t in turns if t["error"]], "answered": any(_answers(c) for c in calls)}


def _answers(call: Mapping[str, Any]) -> bool:
    """A call that answers an input (ch 11 §5; ch 12 §3)."""
    if not call.get("ok"):
        return False
    return call.get("kind") in ("terminal", "routing") or call.get("tool") == "propose_project" or (
        call.get("tool") == "ask_owner" and bool(call.get("blocking")))


def _status(w: Writes, found: Mapping[str, Any], thread: Mapping[str, Any], card: Mapping[str, Any],
            turn_id: str) -> None:
    """The thread's status hop and its ``later`` wake to its coordinator
    (ch 11 §7.5, D37)."""
    coordinator = live_coordinator(w, found["chat_id"])
    if coordinator is None:
        return
    chat = rows.get_chat(w, found["chat_id"])
    wake_id = f"status:{thread['ts']}:{turn_id}"
    rows.add_hop(w, chat_id=found["chat_id"], thread_ts=thread["ts"], kind="status", from_role="thread",
                 from_session=found["session_id"], to_role="project-coordinator", to_session=coordinator,
                 via=["thread"], cause=turn_id, wake_id=wake_id)
    told = {"id": f"status-{turn_id}", "author_kind": "system", "author_id": "thread", "created_at": w.now,
            "text": f"Thread “{thread['title']}” is {card['state']}: {card['happened']}"}
    w.wake(coordinator, Wake(id=wake_id, priority="later", reason="delegation-status", text=envelope.wake(
        reason="delegation-status", chat=chat, trigger=told, now=w.now, thread_ts=thread["ts"], trust="peer",
        mention=False)))
