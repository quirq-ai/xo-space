"""Thread state derived from work, input and control records, never a saved label.

Cards remain the agent's last report. They cannot make an idle open task
look busy. A new input can run after a reported result; explicit resolution
and founder control take precedence over that activity.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from services.cowork_agent.storm.records import intents, tasks
from services.timestamps import parse_ts


def read(conn, thread: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """The API's state plus the reason and deadline for a wait, from one snapshot."""
    def state(value: str, reason: str | None = None, until: str | None = None) -> dict[str, Any]:
        return {"state": value, "wait_reason": reason, "waiting_until": until}

    if thread["resolved_at"]:
        return state("resolved")
    sid, chat, ts = thread["session_id"], thread["chat_id"], thread["ts"]
    if now is None:
        stamp = getattr(conn, "now", None)
        now = parse_ts(stamp).timestamp() if stamp else time.time()
    held = intents.session_run(conn, sid, now) if sid else None
    if held is not None:
        return state("stopped" if held.value == "stopped" else "paused",
                     f"{held.value} by the founder ({held.scope})", held.until)
    session = conn.execute("SELECT status, paused, retired_at FROM sessions WHERE id = ?", (sid,)).fetchone()
    if session and session[1]:
        return state("paused", "session paused")
    task = tasks.of_thread(conn, chat, ts)
    if task and task.state in ("stopped", "cancelled"):
        return state("stopped", f"task {task.state}")
    item = conn.execute("SELECT kind, title, blocking FROM review_items WHERE chat_id = ? AND thread_ts = ? "
                        "AND state = 'open' AND (blocking = 1 OR kind IN ('question', 'approval', 'action', "
                        "'permission', 'create_job', 'create_project')) "
                        "ORDER BY blocking DESC, seq LIMIT 1", (chat, ts)).fetchone()
    if item and item[2]:
        return state("needs_reply" if item[0] == "question" else "needs_approval", item[1])
    if conn.execute("SELECT 1 FROM turns WHERE session_id = ? AND status = 'running' LIMIT 1", (sid,)).fetchone():
        return state("working")
    if conn.execute("SELECT 1 FROM wakes WHERE session_id = ? AND state IN ('queued', 'taken') LIMIT 1",
                    (sid,)).fetchone():
        return state("waiting", "queued input")
    if task and task.state in tasks.ENDED:
        return state(task.state)
    if item:
        return state("needs_reply" if item[0] == "question" else "needs_approval", item[1])
    timer = conn.execute("SELECT note, due_at FROM timers WHERE chat_id = ? AND thread_ts = ? AND state = 'armed' "
                         "ORDER BY due_at IS NULL, due_at, id LIMIT 1", (chat, ts)).fetchone()
    if timer:
        return state("waiting", timer[0], timer[1])
    question = conn.execute("SELECT note FROM hops WHERE from_session = ? AND kind = 'ask' "
                            "AND state <> 'answered' ORDER BY seq LIMIT 1", (sid,)).fetchone()
    if question:
        return state("waiting", question[0] or "answer from coordinator")
    last = conn.execute("SELECT status FROM turns WHERE session_id = ? ORDER BY rowid DESC LIMIT 1",
                        (sid,)).fetchone()
    if last and last[0] == "failed":
        return state("failed")
    card = conn.execute("SELECT card FROM cards WHERE chat_id = ? AND thread_ts = ? ORDER BY seq DESC LIMIT 1",
                        (chat, ts)).fetchone()
    reported = json.loads(card[0]).get("state") if card else None
    if reported == "failed":
        return state("failed")
    if task and task.state == "open":
        return state("stalled", "open task has no running turn or pending wake")
    if reported in ("done", "failed", "needs_reply"):
        return state(reported)
    return state("stalled", "no running turn or pending wake")


class Publisher:
    """Publish changed derived states for existing stream clients; never write a label to threads."""

    def __init__(self, chats):
        self.chats = chats
        self._sessions = set()
        self._places = set()
        self._all = False
        self._task = None

    def committed(self, events):
        for event in events:
            if event.type == "thread":
                continue
            if event.type in ("intent", "pool"):
                self._all = True
            elif event.type in ("session", "turn.started", "turn.finished", "wake", "wake.dropped", "card",
                                "task", "timer", "hop", "review.opened", "review.closed", "review.decided",
                                "review.withdrawn", "review.expired"):
                if event.session_id:
                    self._sessions.add(event.session_id)
                if event.chat_id and event.thread_ts:
                    self._places.add((event.chat_id, event.thread_ts))
        if (self._all or self._sessions or self._places) and (self._task is None or self._task.done()):
            self._task = self.chats.spawn(self._publish(), "thread-states")

    def ephemeral(self, event, epoch):
        pass

    async def _publish(self):
        from services.cowork_agent.storm.chats import rows
        while self._all or self._sessions or self._places:
            all_, sessions, places = self._all, self._sessions, self._places
            self._all, self._sessions, self._places = False, set(), set()

            def publish(w):
                if all_:
                    places.update(w.execute("SELECT chat_id, ts FROM threads").fetchall())
                for sid in sessions:
                    places.update(w.execute("SELECT chat_id, ts FROM threads WHERE session_id = ?", (sid,)))
                for chat, ts in sorted(places):
                    thread = rows.get_thread(w, chat, ts)
                    if thread is None:
                        continue
                    current = {key: thread[key] for key in ("state", "wait_reason", "waiting_until")}
                    previous = w.execute("SELECT data FROM events WHERE type = 'thread' AND chat_id = ? "
                                         "AND thread_ts = ? AND json_extract(data, '$.state') IS NOT NULL "
                                         "ORDER BY seq DESC LIMIT 1", (chat, ts)).fetchone()
                    old = json.loads(previous[0]) if previous else {}
                    if any(old.get(key) != value for key, value in current.items()):
                        w.append("thread", {"ts": ts, **current}, chat_id=chat, thread_ts=ts,
                                 session_id=thread["session_id"])
            await self.chats.engine.write(publish)
