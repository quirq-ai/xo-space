"""A thread's ``activity`` (true state) and API ``state``, derived from the store's records, never saved."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from services.cowork_agent.storm.engine.admission import Wait
from services.cowork_agent.storm.records import intents, tasks
from services.timestamps import parse_ts

if TYPE_CHECKING:
    import sqlite3

    from services.cowork_agent.storm.chats import Chats
    from services.cowork_agent.storm.engine.ports import Writes
    from services.cowork_agent.storm.store import StoredEvent

#: The states the API gives a thread (the ``threads.state`` column's, before it was derived).
API_STATES = ("working", "needs_reply", "needs_approval", "done", "failed", "resolved", "paused")
#: A thread's true states. ``handed_back``: its task's round ended with a result, and it is on call.
ACTIVITIES = ("working", "waiting", "stalled", "handed_back", "done", "failed", "stopped", "paused", "resolved")
#: The keys ``read`` adds to a thread's view, and a ``thread`` event carries when one changed.
KEYS = ("state", "activity", "wait_reason", "waiting_until")
#: Review items that wait for the founder, whether or not they block the agent.
_ASKS = ("question", "approval", "action", "permission", "create_job", "create_project")
#: What a card can report that the API shows while nothing else decides.
_REPORTED = ("done", "needs_reply")


def read(conn: sqlite3.Connection | Writes, thread: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """The thread's ``state`` (the API's word), its ``activity`` (the true
    state), and for a wait its ``wait_reason`` and ``waiting_until``.
    ``thread`` is its row's view; ``now`` is wall seconds (a transaction's
    own time, or the clock's)."""
    if now is None:
        stamp = getattr(conn, "now", None)
        now = parse_ts(stamp).timestamp() if stamp else time.time()
    activity, state, reason, until = _derive(conn, thread, now)
    return {"state": state, "activity": activity, "wait_reason": reason, "waiting_until": until}


def _derive(conn: sqlite3.Connection | Writes, thread: Mapping[str, Any],
            now: float) -> tuple[str, str, str | None, str | None]:
    """(activity, state, wait_reason, waiting_until). The founder's own
    choices come first, then what blocks the agent, then what runs, then
    what is pending, and last what the agent reported. A thread with a task
    is done only by a result, never by its card."""
    if thread["resolved_at"]:
        return "resolved", "resolved", None, None
    sid, chat, ts = thread["session_id"], thread["chat_id"], thread["ts"]
    held = intents.session_run(conn, sid, now) if sid else None
    if held is not None:
        said = f"{held.value} by the founder ({held.scope})"
        return ("stopped", "failed", said, held.until) if held.value == "stopped" else ("paused", "paused", said,
                                                                                         held.until)
    session = conn.execute("SELECT paused, status FROM sessions WHERE id = ?", (sid,)).fetchone()
    if session and session[0]:
        return "paused", "paused", session[0], None      # why, as the session keeps it
    running = bool(
        conn.execute("SELECT 1 FROM turns WHERE session_id = ? AND status = 'running' LIMIT 1", (sid,)).fetchone()
        or conn.execute("SELECT 1 FROM runs WHERE session_id = ? AND state = 'running' LIMIT 1", (sid,)).fetchone()
        # Its input is open (the session's own status, written with each of its changes): a turn ended and
        # the CLI's own next one may follow (a worker's end, a stop's re-injected input). Read as stalled, the
        # thread would flash a request for the founder between two turns of one input.
        or (session is not None and session[1] == "running"))
    queued = conn.execute("SELECT 1 FROM wakes WHERE session_id = ? AND state IN ('queued', 'taken') LIMIT 1",
                          (sid,)).fetchone() is not None
    task = tasks.of_thread(conn, chat, ts)
    if task is not None and task.state in ("stopped", "cancelled"):
        return "stopped", "failed", f"task {task.state}", None
    item = conn.execute(
        f"SELECT kind, title, blocking FROM review_items WHERE chat_id = ? AND thread_ts = ? AND state = 'open' "
        f"AND (blocking = 1 OR kind IN ({', '.join('?' * len(_ASKS))})) ORDER BY blocking DESC, seq LIMIT 1",
        (chat, ts, *_ASKS)).fetchone()
    asked = ("needs_reply" if item[0] == "question" else "needs_approval") if item else None
    if item and item[2]:
        return "waiting", asked, item[1], None
    if running:
        return "working", "working", None, None
    if queued:
        gate = _gate_wait(conn, sid)
        if gate is not None:            # a gate holds it back: what it waits for, and until when
            return "waiting", "working", gate.why, gate.until
        failed = _open_failed(conn, sid)
        if failed is not None:          # its process couldn't open: retried, or held for the founder's next message
            error, retryable = failed
            if retryable:
                return "waiting", "working", f"starting again after: {error}", None
            return "failed", "failed", f"couldn't start: {error}", None
        return "waiting", "working", "queued input", None
    card = conn.execute("SELECT card FROM cards WHERE chat_id = ? AND thread_ts = ? ORDER BY seq DESC LIMIT 1",
                        (chat, ts)).fetchone()
    reported = json.loads(card[0]).get("state") if card else None
    # The engine can commit idle before lifecycle commits the next card.
    if reported in _REPORTED and _card_pending(conn, sid):
        reported = None
    shown = reported if reported in _REPORTED and (task is None or reported != "done") else "working"
    if task is not None and task.state in tasks.ENDED:
        activity = "handed_back" if task.state == "done" else task.state
        return activity, "needs_reply" if (task.state, reported) == ("done", "needs_reply") else task.state, None, None
    if item:
        return "waiting", asked, item[1], None
    timer = conn.execute("SELECT note, due_at FROM timers WHERE chat_id = ? AND thread_ts = ? AND state = 'armed' "
                         "ORDER BY due_at IS NULL, due_at, id LIMIT 1", (chat, ts)).fetchone()
    if timer:
        return "waiting", shown, timer[0], timer[1]
    question = conn.execute("SELECT note FROM hops WHERE from_session = ? AND kind = 'ask' AND state <> 'answered' "
                            "ORDER BY seq LIMIT 1", (sid,)).fetchone()
    if question:
        return "waiting", shown, question[0] or "answer from coordinator", None
    last = conn.execute("SELECT status FROM turns WHERE session_id = ? ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone()
    if reported == "failed" or (last and last[0] == "failed"):
        return "failed", "failed", None, None
    if task is not None:                    # open, and nothing runs or is pending for it
        return "stalled", shown, "open task has no running turn or pending wake", None
    if reported == "done":
        return "done", "done", None, None
    if reported == "needs_reply":
        return "waiting", "needs_reply", "the founder's reply", None
    return "stalled", "working", "no running turn or pending wake", None


def _card_pending(conn: sqlite3.Connection | Writes, sid: str | None) -> bool:
    """Whether the last finished turn still needs its input judged."""
    row = conn.execute(
        "SELECT coalesce(json_array_length(data, '$.wakes_requeued'), 0) FROM events "
        "WHERE session_id = ? AND type = 'turn.finished' AND seq > coalesce(("
        "SELECT json_extract(data, '$.through') FROM events WHERE session_id = ? AND type = 'input.ended' "
        "ORDER BY seq DESC LIMIT 1), 0) ORDER BY seq DESC LIMIT 1", (sid, sid)).fetchone()
    return row is not None and row[0] == 0


def _gate_wait(conn: sqlite3.Connection | Writes, sid: str) -> Wait | None:
    """What a gate holds the ``queued`` session back for (a usage limit, the
    paid gate, near a limit, the founder), as its last ``queued``
    ``session`` event says; None when it isn't ``queued`` or the event is an
    older build's memory wait."""
    status = conn.execute("SELECT status FROM sessions WHERE id = ?", (sid,)).fetchone()
    if not status or status[0] != "queued":
        return None
    row = conn.execute("SELECT data FROM events WHERE session_id = ? AND type = 'session' AND "
                       "json_extract(data, '$.status') = 'queued' AND coalesce(json_extract(data, '$.reason'), '') "
                       "<> 'retired' ORDER BY seq DESC LIMIT 1", (sid,)).fetchone()
    return Wait.from_event(json.loads(row[0])) if row else None


def _open_failed(conn: sqlite3.Connection | Writes, sid: str) -> tuple[str, bool] | None:
    """(error, retryable) when the cold session's process last failed to open, as its last ``session`` event says."""
    status = conn.execute("SELECT status FROM sessions WHERE id = ?", (sid,)).fetchone()
    if not status or status[0] != "cold":
        return None
    row = conn.execute("SELECT data FROM events WHERE session_id = ? AND type = 'session' ORDER BY seq DESC LIMIT 1",
                       (sid,)).fetchone()
    data = json.loads(row[0]) if row else {}
    if data.get("reason") != "open_failed":
        return None
    return str(data.get("error") or "unknown error"), bool(data.get("retryable", True))


#: The events after which a thread's derived state may differ, by the ids they carry.
_MOVES = frozenset({"session", "turn.started", "turn.finished", "run.started", "run.state", "run.finished",
                    "wake.dropped", "wake.moved", "input.ended", "card", "task", "timer", "hop", "review.opened",
                    "review.parked", "review.decided", "review.withdrawn", "review.expired"})


class Publisher:
    """Tells the stream's clients when a thread's derived state changed: a
    ``thread`` event with ``KEYS``, written only when one of them differs
    from the last one this server sent (kept in memory: after a restart a
    thread's first change is sent whether or not it differs, which a client
    merges as it does any other). An engine listener; it never saves a state."""

    def __init__(self, chats: Chats) -> None:
        self.chats = chats
        self._sessions: set[str] = set()
        self._places: set[tuple[str, str]] = set()
        self._chats: set[str] = set()
        self._all = False
        self._sent: dict[tuple[str, str], dict[str, Any]] = {}
        self._task: Any = None

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            if event.type == "intent":
                self._held(event.data)
            elif event.type in _MOVES:
                if event.session_id:
                    self._sessions.add(event.session_id)
                if event.chat_id and event.thread_ts:
                    self._places.add((event.chat_id, event.thread_ts))
        pending = self._all or self._sessions or self._places or self._chats
        if pending and (self._task is None or self._task.done()):
            self._task = self.chats.spawn(self._publish(), "thread-states")

    def ephemeral(self, event: Any, epoch: int) -> None:
        pass

    def _held(self, data: Mapping[str, Any]) -> None:
        """The founder set or lifted a ``run`` choice: the threads in its scope."""
        if data.get("key") != "run":
            return
        scope, scope_id = data.get("scope"), data.get("scope_id") or ""
        if scope == "space":
            self._all = True
        elif scope == "chat":
            self._chats.add(scope_id)
        elif scope == "thread":
            chat_id, _, ts = scope_id.partition("/")
            self._places.add((chat_id, ts))
        elif scope == "session":
            self._sessions.add(scope_id)

    async def _publish(self) -> None:
        from services.cowork_agent.storm.chats import rows       # rows imports this module

        while self._all or self._sessions or self._places or self._chats:
            everything, sessions, places, chats = self._all, self._sessions, self._places, self._chats
            self._all, self._sessions, self._places, self._chats = False, set(), set(), set()

            changed: dict[tuple[str, str], dict[str, Any]] = {}

            def publish(w: Writes) -> None:
                if everything:
                    places.update(w.execute("SELECT chat_id, ts FROM threads WHERE resolved_at IS NULL").fetchall())
                for chat_id in chats:
                    places.update(w.execute("SELECT chat_id, ts FROM threads WHERE chat_id = ? "
                                            "AND resolved_at IS NULL", (chat_id,)).fetchall())
                for sid in sessions:
                    places.update(w.execute("SELECT chat_id, ts FROM threads WHERE session_id = ?", (sid,)).fetchall())
                for chat_id, ts in sorted(places):
                    thread = rows.get_thread(w, chat_id, ts)
                    if thread is None or rows.deleted(w, chat_id, ts) is not None:
                        continue            # a deleted thread says nothing more (its restore sends it whole)
                    current = {key: thread[key] for key in KEYS}
                    if changed.get((chat_id, ts), self._sent.get((chat_id, ts))) != current:
                        changed[(chat_id, ts)] = current
                        w.append("thread", {"ts": ts, **current}, chat_id=chat_id, thread_ts=ts,
                                 session_id=thread["session_id"])
            await self.chats.engine.write(publish)
            self._sent.update(changed)
