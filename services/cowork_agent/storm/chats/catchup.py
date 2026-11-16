"""What a person catching up on a chat reads (v2): what happened in its
threads and when, and when they last looked.

* ``thread_events``: a thread's stored events (or every thread's in a
  chat), oldest first, paged by seq or read from a time on: its state
  changes (``thread`` events carrying ``state``), turns starting and
  ending, files shared, git state read, worktree kept or removed, asks
  opened and decided, and any thread-scoped event a later build adds
  (a steer, an interrupt, a stop): only the families that are a turn's
  inner steps or chatter are left out (``LEFT_OUT``), so a new kind needs
  no change here. Each is ``{seq, at, type, thread_ts, data}``, with
  ``state`` alongside when the event set one.
* ``seen``: the person's mark on a chat, ``{seen_at, seen_seq}``, the same
  on every device. Setting it appends ``chat.seen``, so the person's other
  open clients hear it.
"""

from __future__ import annotations

from typing import Any, Iterable

from services.cowork_agent.storm.chats import Chats, ChatRefused
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.store.db import EVENT_COLUMNS, event_from_row
from services.timestamps import iso, parse_ts

#: Event families that are a turn's steps or a session's plumbing, not what
#: happened to a thread: by prefix and by name. Asked for by name
#: (``types``), they are shown.
LEFT_OUT_PREFIXES = ("step.", "progress.", "run.")
LEFT_OUT = frozenset({"message", "comms.call", "native.input", "input.ended", "card", "hop", "reaction",
                      "session", "chat.seen"})
MAX_LIMIT = 500


def _filter(types: frozenset[str] | None) -> tuple[str, dict[str, Any]]:
    """The SQL that keeps the shown types, and its parameters."""
    if types is not None:
        names = sorted(types)
        return (f"type IN ({', '.join(f':t{i}' for i in range(len(names)))})",
                {f"t{i}": n for i, n in enumerate(names)})
    left = sorted(LEFT_OUT)
    clause = " AND ".join([f"type NOT LIKE '{p}%'" for p in LEFT_OUT_PREFIXES]
                          + [f"type NOT IN ({', '.join(f':t{i}' for i in range(len(left)))})"])
    return clause, {f"t{i}": n for i, n in enumerate(left)}


def event_view(event) -> dict[str, Any]:
    view = {"seq": event.seq, "at": event.ts, "type": event.type, "thread_ts": event.thread_ts,
            "data": dict(event.data)}
    if event.type == "thread" and "state" in event.data:
        view["state"] = event.data["state"]
    return view


async def thread_events(chats: Chats, chat_id: str, *, thread: str | None = None, after_seq: int = 0,
                        since: str | None = None, limit: int = 200,
                        types: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """The chat's (or one thread's) events after ``after_seq`` and, with
    ``since``, at or after that time; oldest first, ``limit`` at most."""
    await chats.chat(chat_id)
    if since is not None:
        when = parse_ts(since)
        if when is None:
            raise ChatRefused("bad_since", f"since must be an ISO-8601 time, not {since[:40]!r}.", status=400)
        since = iso(when)
    clause, params = _filter(frozenset(types) if types else None)
    limit = max(1, min(limit, MAX_LIMIT))

    def read(conn) -> list[dict[str, Any]]:
        if thread is not None and conn.execute("SELECT 1 FROM threads WHERE chat_id = ? AND ts = ?",
                                               (chat_id, thread)).fetchone() is None:
            raise ChatRefused("thread_not_found", f"There is no thread {thread!r} in this chat.", status=404)
        found = conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM events WHERE chat_id = :chat AND thread_ts IS NOT NULL "
            "AND (:thread IS NULL OR thread_ts = :thread) AND seq > :after AND (:since IS NULL OR ts >= :since) "
            f"AND {clause} ORDER BY seq LIMIT :limit",
            {"chat": chat_id, "thread": thread, "after": after_seq, "since": since, "limit": limit, **params}
        ).fetchall()
        return [event_view(event_from_row(row)) for row in found]
    return await chats.engine.store.read(read)


def seen(conn, chat_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT seen_at, seen_seq FROM chat_seen WHERE chat_id = ?", (chat_id,)).fetchone()
    return {"seen_at": row[0], "seen_seq": row[1]} if row else None


async def set_seen(chats: Chats, chat_id: str, *, seen_seq: int | None = None) -> dict[str, Any]:
    """The person looked at the chat now: up to the store's head, or up to
    ``seen_seq`` (what the client had shown). A mark never moves back: a
    slower device sending an older one keeps the newer."""
    await chats.chat(chat_id)

    def save(w: Writes) -> dict[str, Any]:
        head = w.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]
        upto = head if seen_seq is None else min(seen_seq, head)
        current = seen(w, chat_id)
        if current is not None and current["seen_seq"] >= upto:
            return current
        w.execute("INSERT INTO chat_seen (chat_id, seen_at, seen_seq) VALUES (?, ?, ?) ON CONFLICT (chat_id) DO "
                  "UPDATE SET seen_at = excluded.seen_at, seen_seq = excluded.seen_seq", (chat_id, w.now, upto))
        mark = {"seen_at": w.now, "seen_seq": upto}
        w.append("chat.seen", {"id": chat_id, **mark}, chat_id=chat_id)
        return mark
    return await chats.engine.write(save)
