"""What the conversation does when its sessions move (design ch 11 §5, §6.2,
§7.3, §16.2): an engine ``Listener`` that hears the store's commits and the
runtime's ``SessionIdle``, and acts on them in tasks it owns, one session at
a time.

* **An input ends** when the CLI says it has nothing left to do
  (``SessionIdle``: claude may give two results for one input, a worker
  finishing wakes the second), or when its process ends without saying so (a
  crash). Its turns are those finished since the last ``input.ended``; turns
  whose wakes went back to the queue are dropped from it (the input runs
  again). Then, in one transaction with ``input.ended``:
  - **the reply gate** (§5): visible output is a comms call. Over the calls
    its own turns made, a terminal or routing call, a blocking ``ask_owner``
    or a ``propose_project`` answers it. None, and no turn failed or was
    stopped: the gate text goes in once; none after the gate:
    ``turn.unanswered``. Plain chat has no gate;
  - a thread's **card** and state (rule-based, ``cards.py``) and, when it
    replied or failed, a **status** hop with a ``later`` wake to its project
    coordinator;
  - a coordinator's **routing decision** is recorded (``router_decisions``,
    the agent as provider); then it is **recycled** if due.
* **A turn starts:** the hops whose wakes it took are ``delivered``.
* **A session is paused or resumed** (the loop breaker, the person): its
  thread's state follows.
* **At boot** (``recover``), an input that ended while nothing listened is
  judged.

No polling: everything here is driven by the commits and the runtime's events.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from typing import Any, Awaitable, Callable, Mapping, Sequence

from services.cowork_agent.adapters import contract as c
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
#: Every status wake names its thread, and how to answer it (titles repeat).
STATUS_FROM = "From thread “{title}” ({ts}); to answer it, message_thread with thread_id {ts}."
#: With a status wake that carries the owner's own messages in the thread.
STATUS_NOTE = ("The owner wrote the messages above in the thread itself, to its agent: they are the owner's "
               "instructions for that thread, and what the thread did follows them.")
#: A process ended: an input it didn't finish with ``SessionIdle`` ends here.
ENDED = ("cold", "parked")


class Lifecycle:
    def __init__(self, chats: Any) -> None:
        self.chats = chats
        self._locks: dict[str, asyncio.Lock] = {}

    # ── the engine's Listener ────────────────────────────────────────────

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            if event.session_id is None or event.type not in ("session", "turn.started"):
                continue
            if event.type == "turn.started":
                self._spawn(partial(self._delivered, event.session_id, event.turn_id), event.session_id, event.seq)
            elif event.data.get("reason") in ("paused", "resumed"):
                self._spawn(partial(self._paused, event.session_id, event.data["reason"] == "paused"),
                            event.session_id, event.seq)
            elif event.data.get("status") in ENDED:
                self._spawn(partial(self._input_ended, event.session_id), event.session_id, event.seq)

    def ephemeral(self, event: Any, epoch: int) -> None:
        if isinstance(event, c.SessionIdle):
            self._spawn(partial(self._input_ended, event.session), event.session, f"idle:{event.seq}")

    def _spawn(self, work: Callable[[], Awaitable[None]], session_id: str, name: Any) -> None:
        async def serial() -> None:
            async with self._locks.setdefault(session_id, asyncio.Lock()):
                await work()
        self.chats.spawn(serial(), f"{session_id}:{name}")

    async def recover(self) -> None:
        """At boot: inputs that ended while nothing listened (the server
        stopped before it judged them, or their process ended with no retry
        coming) are judged now. A session with wakes still to run is left to
        that run's own end."""
        marks = ", ".join("?" * len(GATED_ROLES))

        def unjudged(conn) -> list[str]:
            return [sid for (sid,) in conn.execute(
                f"SELECT s.id FROM sessions s WHERE s.retired_at IS NULL AND s.role IN ({marks}) AND EXISTS ("
                "SELECT 1 FROM events f WHERE f.session_id = s.id AND f.type = 'turn.finished' AND f.seq > coalesce(("
                "SELECT json_extract(i.data, '$.through') FROM events i WHERE i.session_id = s.id "
                "AND i.type = 'input.ended' ORDER BY i.seq DESC LIMIT 1), 0)) AND NOT EXISTS ("
                "SELECT 1 FROM wakes k WHERE k.session_id = s.id AND k.state IN ('queued', 'taken'))", GATED_ROLES)]
        for session_id in await self.chats.engine.store.read(unjudged):
            self._spawn(partial(self._input_ended, session_id), session_id, "recover")

    # ── an input ended ───────────────────────────────────────────────────

    async def _input_ended(self, session_id: str) -> None:
        engine = self.chats.engine
        found = await engine.store.read(lambda conn: _input(conn, session_id))
        if found is None:
            return
        await self._delivered(session_id, None, turns=[t["id"] for t in found["turns"]])
        role = found["role"]
        statuses = {t["status"] for t in found["turns"]}
        failed, stopped = "failed" in statuses, bool(statuses & set(NOT_ANSWERABLE))
        last = found["turns"][-1]["id"]
        if not found["answered"] and not failed and not stopped:
            # A retired session takes no more input: no gate, it is unanswered.
            if GATE_REASON not in found["reasons"] and not found["retired"]:
                await engine.write(lambda w: (_ended(w, found), self._gate(w, found, last)))
                return
            failed = True
            found["unanswered"] = True
        if role == "thread":
            # The card: stopped is the input's last turn; a thread that handed a question
            # to its coordinator is waiting on it, whatever its checklist says.
            await self._thread_input(found, failed=failed, stopped=found["turns"][-1]["status"] in NOT_ANSWERABLE,
                                     waiting=any(c.get("ok") and c.get("kind") == "routing" for c in found["calls"]),
                                     turn_id=last)
        else:
            await engine.write(lambda w: (_ended(w, found), _decisions(w, found)))
            if not found["retired"]:
                await self.chats.recycle_if_due(await self.chats.chat(found["chat_id"]))

    @staticmethod
    def _gate(w: Writes, found: Mapping[str, Any], turn_id: str) -> None:
        text = GATE_TEXT.format(tools=", ".join(tools.terminal_tools(found["role"])))
        w.wake(found["session_id"], Wake(id=f"gate:{turn_id}", text=text, priority="now", reason=GATE_REASON,
                                         origin="system"))

    async def _thread_input(self, found: Mapping[str, Any], *, failed: bool, stopped: bool, waiting: bool,
                            turn_id: str) -> None:
        chat_id, ts = found["chat_id"], found["thread_ts"]
        error = found["errors"][-1] if found["errors"] else None

        def apply(w: Writes) -> None:
            _ended(w, found)
            thread = rows.get_thread(w, chat_id, ts)
            if thread is None:
                return
            items = w.execute("SELECT title, blocking FROM review_items WHERE chat_id = ? AND thread_ts = ? "
                              "AND state = 'open' ORDER BY seq", (chat_id, ts)).fetchall()
            card = cards.card(thread=thread, replies=found["replies"], failed=failed, error=error,
                              oldest_open_item=items[0][0] if items else None, stopped=stopped, waiting=waiting)
            seq = w.append("card", {"ts": ts, "card": card}, chat_id=chat_id, thread_ts=ts,
                           session_id=found["session_id"], turn_id=turn_id)
            w.execute("INSERT INTO cards (chat_id, thread_ts, seq, card, created_at) VALUES (?, ?, ?, json(?), ?)",
                      (chat_id, ts, seq, json.dumps(card), w.now))
            rows.touch_thread(w, chat_id, ts)
            rows.update_thread(w, chat_id, ts, state=cards.thread_state(thread["state"], card["state"],
                                                                        blocked=any(b for _t, b in items)))
            if found["replies"] or card["state"] == "failed":
                _status(w, found, thread, card, turn_id)
        await self.chats.engine.write(apply)

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


def _input(conn, session_id: str) -> dict[str, Any] | None:
    """The session's input that just ended: the turns finished since the last
    ``input.ended`` (less any that went back to the queue), the comms calls
    those turns made, the wakes they took, the thread's replies. None when
    there is nothing to judge."""
    row = conn.execute("SELECT role, chat_id, thread_ts, retired_at IS NOT NULL FROM sessions WHERE id = ?",
                       (session_id,)).fetchone()
    if row is None or row[0] not in GATED_ROLES:
        return None
    role, chat_id, thread_ts, retired = row
    since = conn.execute("SELECT json_extract(data, '$.through') FROM events WHERE session_id = ? "
                         "AND type = 'input.ended' ORDER BY seq DESC LIMIT 1", (session_id,)).fetchone()
    since = since[0] if since else 0
    through = conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]
    window = (session_id, since, through)
    turns = [{"id": t, "status": s, "error": e, "requeued": bool(json.loads(q or "[]")), "seq": n}
             for n, t, s, e, q in conn.execute(
                 "SELECT seq, turn_id, json_extract(data, '$.status'), json_extract(data, '$.error'), "
                 "json_extract(data, '$.wakes_requeued') FROM events WHERE session_id = ? AND type = 'turn.finished' "
                 "AND seq > ? AND seq <= ? ORDER BY seq", window)]
    # A turn whose wakes went back to the queue doesn't end its input: it runs again.
    redone = [i for i, t in enumerate(turns) if t["requeued"]]
    if redone:
        since, turns = turns[redone[-1]]["seq"], turns[redone[-1] + 1:]
    if not turns:
        return None
    ids = [t["id"] for t in turns]
    marks = ", ".join("?" * len(ids))
    # Only the calls these turns made: a decision model's calls for a
    # coordinator (no turn) answer nothing of the coordinator's (ch 11 §3.1).
    calls = [json.loads(d) for (d,) in conn.execute(
        f"SELECT data FROM events WHERE session_id = ? AND type = 'comms.call' AND turn_id IN ({marks}) ORDER BY seq",
        (session_id, *ids))]
    taken = conn.execute(f"SELECT id, reason FROM wakes WHERE turn_id IN ({marks})", ids).fetchall()
    replies = [text for (text,) in conn.execute(
        "SELECT text FROM messages WHERE author_session_id = ? AND author_kind = 'agent' AND thread_ts IS ? "
        "AND seq > ? AND seq <= ? ORDER BY seq", (session_id, thread_ts, since, through))] if role == "thread" else []
    return {"session_id": session_id, "role": role, "chat_id": chat_id, "thread_ts": thread_ts, "retired": retired,
            "through": through, "turns": turns, "calls": calls, "reasons": {r for _w, r in taken},
            "replies": replies, "messages": [w.removeprefix("msg:") for w, _r in taken if w.startswith("msg:")],
            "decided": [w.split(":")[1] for w, r in taken if r == "review-answered"],
            "errors": [t["error"] for t in turns if t["error"]], "answered": any(_answers(c) for c in calls),
            "unanswered": False}


def _ended(w: Writes, found: Mapping[str, Any]) -> None:
    """``input.ended``: what was judged, so the next input starts after it;
    ``turn.unanswered`` too when the gate was ignored."""
    ids = [t["id"] for t in found["turns"]]
    columns = {"session_id": found["session_id"], "chat_id": found["chat_id"], "thread_ts": found["thread_ts"],
               "turn_id": ids[-1]}
    if found["unanswered"]:
        w.append("turn.unanswered", {"turns": ids}, **columns)
    w.append("input.ended", {"turns": ids, "through": found["through"], "answered": found["answered"]}, **columns)


def _decisions(w: Writes, found: Mapping[str, Any]) -> None:
    """A coordinator's routing of the person's messages it took (ch 11 §3.1)."""
    actions = [{"kind": router.ACTION_OF[c["tool"]], **{k: c[k] for k in ("thread_id", "hop_id", "review_id",
                                                                         "message_id") if c.get(k)}}
               for c in found["calls"] if c.get("ok") and c.get("tool") in router.ACTION_OF]
    for message_id in found["messages"]:
        router.record(w, chat_id=found["chat_id"], message_id=message_id, decided_by="agent",
                      route={"provider": "agent", "actions": actions})


def _answers(call: Mapping[str, Any]) -> bool:
    """A call that answers an input (ch 11 §5; ch 12 §3)."""
    if not call.get("ok"):
        return False
    return call.get("kind") in ("terminal", "routing") or call.get("tool") == "propose_project" or (
        call.get("tool") == "ask_owner" and bool(call.get("blocking")))


def _status(w: Writes, found: Mapping[str, Any], thread: Mapping[str, Any], card: Mapping[str, Any],
            turn_id: str) -> None:
    """The thread's status hop and its ``later`` wake to its coordinator
    (ch 11 §7.5, D37): what the thread reported, and the owner's own
    messages in the thread that this input answered, so the coordinator
    knows what the owner asked the thread directly (§7.2) and never takes
    it for the thread's own idea."""
    coordinator = live_coordinator(w, found["chat_id"])
    if coordinator is None:
        return
    asked = [m for m in (rows.get_message(w, mid) for mid in found["messages"]) if m and m["author_kind"] == "owner"]
    wake_id = f"status:{thread['ts']}:{turn_id}"
    hop = rows.add_hop(w, chat_id=found["chat_id"], thread_ts=thread["ts"], kind="status", from_role="thread",
                       from_session=found["session_id"], to_role="project-coordinator", to_session=coordinator,
                       via=["thread"], cause=turn_id, message_ids=[m["id"] for m in asked], wake_id=wake_id)
    notes = [STATUS_FROM.format(title=thread["title"], ts=thread["ts"])]
    notes += [_decided(w, item_id) for item_id in found["decided"]]
    if asked:
        notes.append(STATUS_NOTE)
    w.wake(coordinator, Wake(id=wake_id, priority="later", reason="delegation-status", text=envelope.relay(
        sender_role="thread", sender_session=found["session_id"], cited=asked, reason="delegation-status",
        note=f"Thread “{thread['title']}” is {card['state']}: {card['happened']}", now=w.now, hop=hop["id"],
        system_note=" ".join(n for n in notes if n))))


def _decided(w: Writes, item_id: str) -> str:
    """What the owner decided on a review item the thread's input carried out."""
    row = w.execute("SELECT title, decision FROM review_items WHERE id = ?", (item_id,)).fetchone()
    if row is None or row[1] is None:
        return ""
    decision = json.loads(row[1])
    said = decision.get("answer") or decision.get("option") or decision.get("decision")
    return f"The owner decided review item {item_id} (“{row[0]}”): {said}. This input followed that decision."
