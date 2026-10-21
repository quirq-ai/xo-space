"""The conversation's rows (design ch 08 §2, ch 11, ch 11 §16.2): chats,
messages, threads and hops, each written with its event in the caller's
transaction (``Writes``), and read as the routes and the tools show them.

Runs on the store's thread: plain SQL, no awaiting. The events are the
stream's state vocabulary (ch 09 §3):

* ``message`` — a new message, its whole view (the seq is its order);
* ``thread`` — ``{"ts", …}``: the whole view when a thread is created, then
  only the columns that changed;
* ``hop`` — a hop, without its note (the route shows the note), and
  ``{"id", "state"}`` when its state moves;
* ``reaction`` — ``{"message_id", "emoji", "by"}``.

The text an agent or the person writes (a message, a hop's note, a thread's
title and status) is stored redacted (ch 16 §3), so a row and its event say
the same.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Mapping, Sequence

from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.redact import redact, redact_data

ID_HEX = 16
WORKSPACE = "workspace"
PROJECT = "project"
THREAD_STATES = ("working", "needs_reply", "needs_approval", "done", "failed", "resolved", "paused")
HOP_KINDS = ("forward", "relay", "brief", "ask", "status", "answer")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:ID_HEX]}"


# ── chats ────────────────────────────────────────────────────────────────────

CHAT_COLUMNS = "id, kind, project_pid, project_name, created_at, settings, retired_at"
#: A retired chat's key: it gives up its project's key, so a new project of that name gets a chat of its own.
RETIRED_KEY = "retired:{id}"


def chat_view(row: Sequence[Any]) -> dict[str, Any]:
    chat_id, kind, pid, name, created_at, settings, retired_at = row
    return {"id": chat_id, "kind": kind, "project_pid": None if retired_at else pid, "project": name,
            "created_at": created_at, "settings": json.loads(settings), "retired_at": retired_at}


def get_chat(conn, chat_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {CHAT_COLUMNS} FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return chat_view(row) if row else None


def workspace_chat(w: Writes) -> dict[str, Any]:
    """The one workspace chat, created the first time it is needed."""
    row = w.execute(f"SELECT {CHAT_COLUMNS} FROM chats WHERE kind = 'workspace'").fetchone()
    if row is None:
        w.execute("INSERT INTO chats (id, kind, created_at) VALUES (?, 'workspace', ?)", (new_id("chat"), w.now))
        row = w.execute(f"SELECT {CHAT_COLUMNS} FROM chats WHERE kind = 'workspace'").fetchone()
    return chat_view(row)


def project_chat(w: Writes, *, name: str, pid: str | None) -> tuple[dict[str, Any], bool]:
    """A project's chat, created the first time (ch 08 §5: its id is assigned
    once). It is keyed by the project's pid, or by its folder name until the
    pid exists, and then moved onto the pid once. Returns the chat and
    whether it was just created."""
    key = pid or name
    row = w.execute(f"SELECT {CHAT_COLUMNS} FROM chats WHERE kind = 'project' AND (project_pid = ? "
                    "OR (project_pid = project_name AND project_name = ?))", (key, name)).fetchone()
    created = row is None
    if created:
        w.execute("INSERT INTO chats (id, kind, project_pid, project_name, created_at) VALUES (?, 'project', ?, ?, ?)",
                  (new_id("chat"), key, name, w.now))
    elif (row[2], row[3]) != (key, name):           # the pid appeared, or the folder was renamed
        w.execute("UPDATE chats SET project_pid = ?, project_name = ? WHERE id = ?", (key, name, row[0]))
    row = w.execute(f"SELECT {CHAT_COLUMNS} FROM chats WHERE project_pid = ?", (key,)).fetchone()
    return chat_view(row), created


def retire_project_chat(w: Writes, *, name: str, pid: str | None) -> tuple[str, list[str]] | None:
    """Its project was removed through XO (§0.102): the chat is kept,
    readable by its id, but gives up its project's key, and its live sessions
    are retired with it. Returns the chat's id and the sessions retired, or
    None when the project had no live chat."""
    row = w.execute("SELECT id FROM chats WHERE kind = 'project' AND retired_at IS NULL AND (project_name = ? "
                    "OR (? IS NOT NULL AND project_pid = ?)) ORDER BY project_name = ? DESC LIMIT 1",
                    (name, pid, pid, name)).fetchone()
    if row is None:
        return None
    chat_id = row[0]
    w.execute("UPDATE chats SET retired_at = ?, project_pid = ? WHERE id = ?",
              (w.now, RETIRED_KEY.format(id=chat_id), chat_id))
    w.append("chat", {"id": chat_id, "retired_at": w.now, "why": "project_removed"}, chat_id=chat_id)
    sessions = []
    for session_id, status, epoch in w.execute(
            "UPDATE sessions SET retired_at = ?, updated_at = ? WHERE chat_id = ? AND retired_at IS NULL "
            "RETURNING id, status, epoch", (w.now, w.now, chat_id)).fetchall():
        w.append("session", {"status": status, "reason": "retired", "why": "project_removed"},
                 session_id=session_id, chat_id=chat_id, epoch=epoch)
        sessions.append(session_id)
    return chat_id, sessions


# ── messages ─────────────────────────────────────────────────────────────────

MESSAGE_COLUMNS = ("id, seq, chat_id, thread_ts, author_kind, author_id, author_session_id, text, reply_to, "
                   "client_id, extra, created_at")


def message_view(row: Sequence[Any]) -> dict[str, Any]:
    (message_id, seq, chat_id, thread_ts, author_kind, author_id, author_session_id, text, reply_to, _client_id,
     extra, created_at) = row
    return {"id": message_id, "seq": seq, "chat_id": chat_id, "thread_ts": thread_ts, "author_kind": author_kind,
            "author_id": author_id, "author_session_id": author_session_id, "text": text, "reply_to": reply_to,
            "extra": json.loads(extra), "created_at": created_at}


def get_message(conn, message_id: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE id = ?", (message_id,)).fetchone()
    return message_view(row) if row else None


def add_message(w: Writes, *, chat_id: str, author_kind: str, text: str, thread_ts: str | None = None,
                author_id: str | None = None, author_session_id: str | None = None, reply_to: str | None = None,
                extra: Mapping[str, Any] | None = None, client_id: str | None = None) -> dict[str, Any]:
    """One message with its ``message`` event. With a ``client_id`` already
    used in this chat, the stored message is returned and nothing is
    written (a retried POST stores one message)."""
    if client_id is not None:
        row = w.execute(f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? AND client_id = ?",
                        (chat_id, client_id)).fetchone()
        if row is not None:
            return message_view(row)
    message_id = new_id("cmsg")
    text, extra = redact(text), redact_data(dict(extra or {}))
    view = {"id": message_id, "chat_id": chat_id, "thread_ts": thread_ts, "author_kind": author_kind,
            "author_id": author_id, "author_session_id": author_session_id, "text": text, "reply_to": reply_to,
            "extra": dict(extra or {}), "created_at": w.now}
    seq = w.append("message", view, chat_id=chat_id, thread_ts=thread_ts, session_id=author_session_id)
    if thread_ts is not None:
        touch_thread(w, chat_id, thread_ts)
    w.execute(f"INSERT INTO messages ({MESSAGE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, json(?), ?)",
              (message_id, seq, chat_id, thread_ts, author_kind, author_id, author_session_id, text, reply_to,
               client_id, json.dumps(view["extra"]), w.now))
    return {**view, "seq": seq}


def add_reaction(w: Writes, message: Mapping[str, Any], *, emoji: str, by: str) -> None:
    extra = dict(message["extra"])
    extra["reactions"] = [*extra.get("reactions", []), {"emoji": emoji, "by": by, "at": w.now}]
    w.execute("UPDATE messages SET extra = json(?) WHERE id = ?", (json.dumps(extra), message["id"]))
    w.append("reaction", {"message_id": message["id"], "emoji": emoji, "by": by}, chat_id=message["chat_id"],
             thread_ts=message["thread_ts"])


def page_messages(conn, *, chat_id: str, thread_ts: str | None, timeline: bool, after_seq: int = 0,
                  before_seq: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
    """A page of a timeline (``timeline``: messages outside any thread) or of
    one thread, by seq, oldest first: the ``limit`` after ``after_seq``, or,
    with ``before_seq`` alone, the ``limit`` right before it."""
    backwards = before_seq is not None and not after_seq
    where = "thread_ts IS NULL" if timeline else "thread_ts = :thread"
    rows = conn.execute(
        f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE chat_id = :chat AND {where} AND seq > :after "
        f"AND (:before IS NULL OR seq < :before) ORDER BY seq {'DESC' if backwards else 'ASC'} LIMIT :limit",
        {"chat": chat_id, "thread": thread_ts, "after": after_seq, "before": before_seq, "limit": limit}).fetchall()
    return [message_view(r) for r in (reversed(rows) if backwards else rows)]


def latest_messages(conn, *, chat_id: str, thread_ts: str | None, limit: int) -> list[dict[str, Any]]:
    """The newest ``limit`` messages of a timeline (``thread_ts`` None) or a
    thread, oldest first."""
    rows = conn.execute(f"SELECT {MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? AND thread_ts IS ? "
                        "ORDER BY seq DESC LIMIT ?", (chat_id, thread_ts, limit)).fetchall()
    return [message_view(r) for r in reversed(rows)]


# ── threads ──────────────────────────────────────────────────────────────────

THREAD_COLUMNS = ("chat_id, ts, title, label, state, resolved_at, session_id, cwd, branch, worktree_path, "
                  "base_commit, status_md, outputs, created_at, updated_at")


def thread_view(row: Sequence[Any]) -> dict[str, Any]:
    (chat_id, ts, title, label, state, resolved_at, session_id, cwd, branch, worktree_path, base_commit, status_md,
     outputs, created_at, updated_at) = row
    return {"chat_id": chat_id, "ts": ts, "title": title, "label": label, "state": state, "resolved_at": resolved_at,
            "session_id": session_id, "cwd": cwd, "branch": branch, "worktree_path": worktree_path,
            "base_commit": base_commit, "status_md": status_md, "outputs": json.loads(outputs),
            "created_at": created_at, "updated_at": updated_at}


def get_thread(conn, chat_id: str, ts: str) -> dict[str, Any] | None:
    row = conn.execute(f"SELECT {THREAD_COLUMNS} FROM threads WHERE chat_id = ? AND ts = ?", (chat_id, ts)).fetchone()
    return thread_view(row) if row else None


def list_threads(conn, chat_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    """A chat's threads, the most recently active first."""
    rows = conn.execute(f"SELECT {THREAD_COLUMNS} FROM threads WHERE chat_id = ? ORDER BY updated_at DESC, ts DESC "
                        "LIMIT ?", (chat_id, -1 if limit is None else limit)).fetchall()
    return [thread_view(r) for r in rows]


def add_thread(w: Writes, *, chat_id: str, ts: str, title: str, session_id: str, cwd: str) -> dict[str, Any]:
    title = redact(title)
    w.execute("INSERT INTO threads (chat_id, ts, title, state, session_id, cwd, created_at, updated_at) "
              "VALUES (?, ?, ?, 'working', ?, ?, ?, ?)", (chat_id, ts, title, session_id, cwd, w.now, w.now))
    view = get_thread(w, chat_id, ts)
    w.append("thread", view, chat_id=chat_id, thread_ts=ts, session_id=session_id)
    return view


#: The thread's columns that hold words an agent wrote.
REDACTED_THREAD_COLUMNS = ("title", "status_md")


def update_thread(w: Writes, chat_id: str, ts: str, **changes: Any) -> dict[str, Any]:
    """Set the changed columns and append ``thread {ts, …changes}``; a no-op
    change writes nothing. Returns the thread as it now is."""
    before = get_thread(w, chat_id, ts)
    changes.update({k: redact(changes[k]) for k in REDACTED_THREAD_COLUMNS if changes.get(k)})
    changed = {k: v for k, v in changes.items() if before[k] != v}
    if changed:
        sets = ", ".join(f"{k} = json(?)" if k == "outputs" else f"{k} = ?" for k in changed)
        w.execute(f"UPDATE threads SET {sets}, updated_at = ? WHERE chat_id = ? AND ts = ?",
                  (*(json.dumps(v) if k == "outputs" else v for k, v in changed.items()), w.now, chat_id, ts))
        w.append("thread", {"ts": ts, **changed}, chat_id=chat_id, thread_ts=ts, session_id=before["session_id"])
    return {**before, **changed}


def touch_thread(w: Writes, chat_id: str, ts: str) -> None:
    """Something happened in the thread (a message, a card): its last
    activity moves, which the threads list sorts by and handoffs show."""
    w.execute("UPDATE threads SET updated_at = ? WHERE chat_id = ? AND ts = ?", (w.now, chat_id, ts))


def latest_card(conn, chat_id: str, ts: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT card FROM cards WHERE chat_id = ? AND thread_ts = ? ORDER BY seq DESC LIMIT 1",
                       (chat_id, ts)).fetchone()
    return json.loads(row[0]) if row else None


# ── hops ─────────────────────────────────────────────────────────────────────

HOP_COLUMNS = ("id, seq, chat_id, thread_ts, kind, from_role, from_session, to_role, to_session, via, cause, message_ids, "
               "note, wake_id, state, created_at, delivered_at, answered_at")


def hop_view(row: Sequence[Any]) -> dict[str, Any]:
    (hop_id, seq, chat_id, thread_ts, kind, from_role, from_session, to_role, to_session, via, cause, message_ids, note,
     wake_id, state, created_at, delivered_at, answered_at) = row
    return {"id": hop_id, "seq": seq, "chat_id": chat_id, "thread_ts": thread_ts, "kind": kind, "from_role": from_role,
            "from_session": from_session, "to_role": to_role, "to_session": to_session, "via": json.loads(via),
            "cause": cause, "message_ids": json.loads(message_ids), "note": note, "wake_id": wake_id, "state": state,
            "created_at": created_at, "delivered_at": delivered_at, "answered_at": answered_at}


#: How far down the line each role sits: hops go down to work, up to report.
LEVEL = {"workspace-coordinator": 0, "project-coordinator": 1, "thread": 2}


def add_hop(w: Writes, *, chat_id: str, thread_ts: str | None, kind: str, from_role: str, from_session: str | None,
            to_role: str, to_session: str | None, via: Iterable[str] = (), cause: str | None = None,
            message_ids: Iterable[str] = (), note: str | None = None, wake_id: str | None = None,
            hop_id: str | None = None) -> dict[str, Any]:
    """One hop (ch 11 §16.2), ``sent``, with its ``hop`` event; sent up the
    line, it answers the delivered hops that came down it from its receiver.
    Pass ``hop_id`` when the wake's id is made from it."""
    hop_id = hop_id or new_id("hop")
    note = redact(note) if note else note
    view = {"id": hop_id, "chat_id": chat_id, "thread_ts": thread_ts, "kind": kind, "from_role": from_role,
            "from_session": from_session, "to_role": to_role, "to_session": to_session, "via": list(via),
            "cause": cause, "message_ids": list(message_ids), "note": note, "wake_id": wake_id, "state": "sent",
            "created_at": w.now, "delivered_at": None, "answered_at": None}
    seq = w.append("hop", {k: v for k, v in view.items() if k != "note"}, chat_id=chat_id, thread_ts=thread_ts,
                   session_id=from_session)
    w.execute(f"INSERT INTO hops ({HOP_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, json(?), ?, json(?), ?, ?, ?, ?, "
              "NULL, NULL)", (hop_id, seq, chat_id, thread_ts, kind, from_role, from_session, to_role, to_session,
                              json.dumps(view["via"]), cause, json.dumps(view["message_ids"]), note, wake_id, "sent",
                              w.now))
    if from_session and to_session and LEVEL.get(from_role, -1) > LEVEL.get(to_role, LEVEL["thread"] + 1):
        # A report back up the line answers what came down it and was taken
        # (ch 11 §16.2): a thread's status or message answers its brief and
        # the coordinator's follow-ups to it. Questions sent up (an ask, a
        # thread's question, ask_workspace) are answered by the call that
        # answers them.
        for (earlier,) in w.execute("SELECT id FROM hops WHERE from_session = ? AND to_session = ? "
                                    "AND state = 'delivered'", (to_session, from_session)).fetchall():
            set_hop_state(w, earlier, "answered")
    return {**view, "seq": seq}


def set_hop_state(w: Writes, hop_id: str, state: str) -> bool:
    """Move a hop on (``sent`` → ``delivered`` → ``answered``, never back)."""
    column = {"delivered": "delivered_at", "answered": "answered_at"}[state]
    allowed = ("sent",) if state == "delivered" else ("sent", "delivered")
    row = w.execute(f"UPDATE hops SET state = ?, {column} = ? WHERE id = ? AND state IN ({', '.join('?' * len(allowed))}) "
                    "RETURNING chat_id, thread_ts", (state, w.now, hop_id, *allowed)).fetchone()
    if row is None:
        return False
    w.append("hop", {"id": hop_id, "state": state}, chat_id=row[0], thread_ts=row[1])
    return True
