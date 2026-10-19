"""The tools agents speak, route and read with (design ch 12 §3–§5, §5.9;
the PoC's ``comms.py`` and ``workspace.py``).

Scope comes from the caller's session, never from an argument (ch 12 §6): a
coordinator acts on its own chat and that chat's threads; a thread on its
own thread (and reads its root message). Owner messages an agent cites are
copied by the server from the store, and only owner rows of the caller's own
scope can be cited. Every hop between agents is recorded with its wake in
the same transaction (ch 11 §16.2); its wake id is ``deleg:<hop id>``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from services.cowork_agent.storm import chats as chats_pkg
from services.cowork_agent.storm.chats import envelope, rows
from services.cowork_agent.storm.comms import args as a
from services.cowork_agent.storm.comms.calls import CommsError, ToolCall
from services.cowork_agent.storm.comms.tools import (
    EMOJI_MAX,
    LABEL_MAX,
    LINE_MAX,
    LONG_TEXT_MAX,
    MAX_CITED,
    MAX_FETCH_IDS,
    SHORT_LINE_MAX,
    TITLE_MAX,
    WORD_MAX,
)
from services.cowork_agent.storm.engine import Wake, Writes
from services.cowork_agent.storm.presets import Intelligence

#: How a hop between agents queues its receiver's input (ch 07 §4.1).
HANDOFF_PRIORITY = "next"
FETCH_TIMELINE = 30
FETCH_THREAD = 20
OPEN_THREADS_LISTED = 10
THREADS_LISTED = 20
EFFORTS = ("low", "medium", "high", "xhigh", "max")


# ── scope ────────────────────────────────────────────────────────────────────

def _in_scope(t: ToolCall, message: Mapping[str, Any] | None) -> bool:
    caller = t.caller
    if message is None or message["chat_id"] != caller.chat_id:
        return False
    if caller.role == "thread":
        return message["thread_ts"] == caller.thread_ts or message["id"] == caller.thread_ts
    return True


def _message(w: Writes, t: ToolCall, message_id: str) -> dict[str, Any]:
    message = rows.get_message(w, message_id)
    if not _in_scope(t, message):
        raise CommsError("no_such_message", f"There is no message {message_id!r} you can reach.")
    return message


def _cited(w: Writes, t: ToolCall, message_ids: list[str]) -> list[dict[str, Any]]:
    """The owner's own messages, copied from the store (ch 12 §6)."""
    cited = [_message(w, t, m) for m in message_ids]
    if any(m["author_kind"] != "owner" for m in cited):
        raise CommsError("not_an_owner_message", "Only the owner's own messages can be cited.")
    return cited


def _thread(w: Writes, t: ToolCall, thread_id: str | None) -> dict[str, Any]:
    """The thread a call is about: a thread's own, or one of the
    coordinator's chat."""
    caller = t.caller
    if caller.role == "thread":
        if thread_id and thread_id != caller.thread_ts:
            raise CommsError("not_your_thread", "A thread can only act on its own thread.")
        thread_id = caller.thread_ts
    elif not thread_id:
        raise CommsError("missing_argument", "thread_id is required.")
    thread = rows.get_thread(w, caller.chat_id, thread_id)
    if thread is None:
        raise CommsError("no_such_thread", f"There is no thread {thread_id!r} in this chat.")
    return thread


def _brief(message: Mapping[str, Any]) -> dict[str, Any]:
    out = {"id": message["id"], "author_kind": message["author_kind"], "author_id": message["author_id"],
           "written_at": message["created_at"], "text": message["text"], "thread_ts": message["thread_ts"]}
    if forwarded := message["extra"].get("forwarded_from"):
        out["forwarded_from"] = forwarded
    return out


def _thread_row(w: Writes, thread: Mapping[str, Any]) -> dict[str, Any]:
    card = rows.latest_card(w, thread["chat_id"], thread["ts"]) or {}
    return {"thread_id": thread["ts"], "title": thread["title"], "label": thread["label"], "state": thread["state"],
            "headline": card.get("headline"), "needs_you": card.get("needs_you"),
            "last_activity_at": thread["updated_at"]}


def _handoff(w: Writes, session_id: str, hop_id: str, text: str, reason: str) -> None:
    w.wake(session_id, Wake(id=f"deleg:{hop_id}", text=text, priority=HANDOFF_PRIORITY, reason=reason))


# ── speaking ─────────────────────────────────────────────────────────────────

async def post_message(t: ToolCall) -> dict[str, Any]:
    text = a.text(t.args, "text")
    return await t.write(lambda w: {"message_id": rows.add_message(
        w, chat_id=t.caller.chat_id, author_kind="agent", author_id=t.caller.role,
        author_session_id=t.caller.session_id, text=text)["id"]})


async def reply(t: ToolCall) -> dict[str, Any]:
    text = a.text(t.args, "text")
    caller = t.caller

    def fn(w: Writes) -> dict[str, Any]:
        message = rows.add_message(w, chat_id=caller.chat_id, thread_ts=caller.thread_ts, author_kind="agent",
                                   author_id="thread", author_session_id=caller.session_id, text=text)
        return {"message_id": message["id"], "thread_id": caller.thread_ts}
    return await t.write(fn)


async def update_status(t: ToolCall) -> dict[str, Any]:
    text = a.text(t.args, "text", limit=LONG_TEXT_MAX)
    caller = t.caller

    def fn(w: Writes) -> dict[str, Any]:
        rows.update_thread(w, caller.chat_id, caller.thread_ts, status_md=text)
        return {"ok": True, "thread_id": caller.thread_ts}
    return await t.write(fn)


async def react(t: ToolCall) -> dict[str, Any]:
    message_id, emoji = a.text(t.args, "message_id"), a.text(t.args, "emoji", limit=EMOJI_MAX)

    def fn(w: Writes) -> dict[str, Any]:
        rows.add_reaction(w, _message(w, t, message_id), emoji=emoji, by=t.caller.session_id)
        return {"ok": True, "message_id": message_id}
    return await t.write(fn)


async def no_reply_needed(t: ToolCall) -> dict[str, Any]:
    a.text(t.args, "reason", limit=WORD_MAX)
    return await t.write(lambda w: {"ok": True})


async def no_such_project(t: ToolCall) -> dict[str, Any]:
    return await post_message(t)


# ── routing ──────────────────────────────────────────────────────────────────

async def forward_to_project(t: ToolCall) -> dict[str, Any]:
    """The workspace coordinator hands the owner's words to a project's
    coordinator; with only a note, it answers that coordinator's question
    (the hop is an ``answer`` to the ``ask``, ch 12 §4)."""
    chats = t.chats
    name = a.text(t.args, "project")
    message_ids = a.ids(t.args, "context_message_ids", limit=MAX_CITED)
    note = a.text(t.args, "note", required=False)
    if not message_ids and not note:
        raise CommsError("empty_forward", "Pass the owner's message ids in context_message_ids, a note, or both.")
    if name not in await asyncio.to_thread(chats_pkg.project_folders):
        raise CommsError("no_such_project", f"There is no project {name!r}. Use list_projects, or propose_project "
                                            "for new work.")
    target = await chats.project(name)
    workspace_session = t.caller.session_id

    def fn(w: Writes, coordinator: str) -> dict[str, Any]:
        originals = _cited(w, t, message_ids)
        copies = [rows.add_message(w, chat_id=target["id"], author_kind="owner", author_id=m["author_id"],
                                   text=m["text"], extra={"forwarded_from": {"chat_id": m["chat_id"],
                                                                             "message_id": m["id"],
                                                                             "via": ["workspace"]}})
                  for m in originals]
        question = None if copies else _open_hop(w, chat_id=target["id"], kind="ask", to_role="workspace-coordinator")
        kind = "forward" if copies else "answer" if question else "relay"
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=target["id"], thread_ts=question["thread_ts"] if question else None,
                     kind=kind, from_role="workspace-coordinator", from_session=workspace_session,
                     to_role="project-coordinator", to_session=coordinator, via=["workspace"],
                     cause=originals[0]["id"] if originals else question["id"] if question else None,
                     message_ids=[m["id"] for m in copies], note=note, wake_id=f"deleg:{hop_id}")
        if question:
            rows.set_hop_state(w, question["id"], "answered")
        parts = [envelope.relay(sender_role="workspace-coordinator", sender_session=workspace_session, note=note,
                                reason=kind, now=w.now, hop=hop_id)] if note else []
        parts += [envelope.wake(reason="mention", chat=target, trigger=copy, now=w.now) for copy in copies]
        _handoff(w, coordinator, hop_id, "\n\n".join(parts), kind)
        return t.record(w, {"project": name, "hop_id": hop_id, "forwarded_message_ids": [m["id"] for m in copies]})
    return await chats.with_coordinator(target, fn)


def _open_hop(w: Writes, *, chat_id: str, kind: str, to_role: str, from_session: str | None = None,
              thread_ts: str | None = None) -> dict[str, Any] | None:
    """The latest hop of ``kind`` to ``to_role`` that hasn't been answered."""
    row = w.execute(f"SELECT {rows.HOP_COLUMNS} FROM hops WHERE chat_id = ? AND kind = ? AND to_role = ? "
                    "AND state <> 'answered' AND (? IS NULL OR from_session = ?) AND (? IS NULL OR thread_ts = ?) "
                    "ORDER BY seq DESC LIMIT 1",
                    (chat_id, kind, to_role, from_session, from_session, thread_ts, thread_ts)).fetchone()
    return rows.hop_view(row) if row else None


async def start_thread_session(t: ToolCall) -> dict[str, Any]:
    chats, caller = t.chats, t.caller
    anchor_id = a.text(t.args, "message_id")
    title = a.text(t.args, "title", limit=TITLE_MAX)
    instructions = a.text(t.args, "instructions")
    extra_ids = [m for m in a.ids(t.args, "context_message_ids", limit=MAX_CITED) if m != anchor_id]
    ack = a.text(t.args, "ack", required=False, limit=LINE_MAX)
    project_ack = a.text(t.args, "project_ack", required=False, limit=SHORT_LINE_MAX)
    intelligence = Intelligence(a.text(t.args, "model", required=False, limit=WORD_MAX),
                                a.choice(t.args, "effort", EFFORTS))
    chat = await chats.chat(caller.chat_id)

    def anchored(conn) -> dict[str, Any] | None:
        message = rows.get_message(conn, anchor_id)
        if message is None or message["chat_id"] != chat["id"] or message["thread_ts"] is not None:
            raise CommsError("no_such_message", f"{anchor_id!r} is not a message on this chat's timeline.")
        if rows.get_thread(conn, chat["id"], anchor_id) is not None:
            raise CommsError("thread_exists", f"Message {anchor_id} already has a thread; send it a follow-up with "
                                              "message_thread.")
        return message
    anchor = await t.engine.store.read(anchored)

    def also(w: Writes, thread_session: str) -> dict[str, Any]:
        cited = _cited(w, t, extra_ids)
        if project_ack:
            rows.add_message(w, chat_id=chat["id"], author_kind="agent", author_id=caller.role,
                             author_session_id=caller.session_id, text=project_ack)
        if ack:
            rows.add_message(w, chat_id=chat["id"], thread_ts=anchor_id, author_kind="agent", author_id=caller.role,
                             author_session_id=caller.session_id, text=ack)
        via = [*(anchor["extra"].get("forwarded_from") or {}).get("via", []), "project"]
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=chat["id"], thread_ts=anchor_id, kind="brief",
                     from_role=caller.role, from_session=caller.session_id, to_role="thread",
                     to_session=thread_session, via=via, cause=anchor_id,
                     message_ids=[anchor_id, *(m["id"] for m in cited)], note=instructions,
                     wake_id=f"deleg:{hop_id}")
        # One wake, so the brief and the owner's message reach the thread in one turn (D34).
        brief = envelope.relay(sender_role=caller.role, sender_session=caller.session_id, cited=cited,
                               note=instructions, reason="spawn", now=w.now, hop=hop_id)
        first = envelope.wake(reason="mention", chat=chat, trigger=anchor, now=w.now, role="initiator")
        _handoff(w, thread_session, hop_id, f"{brief}\n\n{first}", "spawn")
        return t.record(w, {"thread_id": anchor_id, "session_id": thread_session, "hop_id": hop_id})
    return await chats.start_thread(chat, anchor_id, title, intelligence, also)


async def message_thread(t: ToolCall) -> dict[str, Any]:
    """A follow-up to one of the coordinator's threads: it reopens a resolved
    thread, and answers the thread's own open question to it."""
    caller = t.caller
    thread_id = a.text(t.args, "thread_id")
    message_ids = a.ids(t.args, "context_message_ids", limit=MAX_CITED)
    note = a.text(t.args, "note", required=False)
    if not message_ids and not note:
        raise CommsError("empty_relay", "Pass the owner's message in context_message_ids, a note, or both.")

    def fn(w: Writes) -> dict[str, Any]:
        thread = _thread(w, t, thread_id)
        cited = _cited(w, t, message_ids)
        reopened = thread["state"] == "resolved"
        if reopened:
            rows.update_thread(w, caller.chat_id, thread_id, state="working", resolved_at=None)
        question = _open_hop(w, chat_id=caller.chat_id, kind="relay", to_role="project-coordinator",
                             from_session=thread["session_id"], thread_ts=thread_id)
        kind = "answer" if question else "relay"
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=caller.chat_id, thread_ts=thread_id, kind=kind,
                     from_role=caller.role, from_session=caller.session_id, to_role="thread",
                     to_session=thread["session_id"], via=["project"],
                     cause=question["id"] if question else (cited[0]["id"] if cited else None),
                     message_ids=[m["id"] for m in cited], note=note, wake_id=f"deleg:{hop_id}")
        if question:
            rows.set_hop_state(w, question["id"], "answered")
        _handoff(w, thread["session_id"], hop_id, envelope.relay(
            sender_role=caller.role, sender_session=caller.session_id, cited=cited, note=note, reason=kind,
            now=w.now, hop=hop_id), kind)
        return {"delivered": True, "thread_id": thread_id, "reopened": reopened, "hop_id": hop_id}
    return await t.write(fn)


async def ask_workspace(t: ToolCall) -> dict[str, Any]:
    """A project coordinator asks the workspace coordinator, maybe on a
    thread's behalf; the answer comes back as a ``forward_to_project``
    note (an ``answer`` hop)."""
    chats, caller = t.chats, t.caller
    question = a.text(t.args, "question", limit=LONG_TEXT_MAX)
    on_behalf = a.text(t.args, "on_behalf_of", required=False)
    project = await chats.chat(caller.chat_id)
    workspace = await chats.workspace()

    def fn(w: Writes, coordinator: str) -> dict[str, Any]:
        thread = _thread(w, t, on_behalf) if on_behalf else None
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=project["id"], thread_ts=on_behalf, kind="ask",
                     from_role=caller.role, from_session=caller.session_id, to_role="workspace-coordinator",
                     to_session=coordinator, via=["thread", "project"] if thread else ["project"], note=question,
                     wake_id=f"deleg:{hop_id}")
        who = f" on behalf of its thread “{thread['title']}”" if thread else ""
        _handoff(w, coordinator, hop_id, envelope.relay(
            sender_role=caller.role, sender_session=caller.session_id, note=question, reason="ask", now=w.now,
            hop=hop_id, system_note=f"The coordinator of project “{project['project']}” asks this{who}. Answer it "
                                    f"with forward_to_project to “{project['project']}”, a note only."), "ask")
        return t.record(w, {"asked": True, "hop_id": hop_id,
                            "note": "The answer will arrive as a relay. End your turn now."})
    return await chats.with_coordinator(workspace, fn)


async def message_coordinator(t: ToolCall) -> dict[str, Any]:
    """A thread hands a question or an update to its own project
    coordinator; it can name no one else (ch 12 §5.1)."""
    chats, caller = t.chats, t.caller
    text = a.text(t.args, "text")
    message_ids = a.ids(t.args, "context_message_ids", limit=MAX_CITED)
    chat = await chats.chat(caller.chat_id)

    def fn(w: Writes, coordinator: str) -> dict[str, Any]:
        thread = _thread(w, t, None)
        cited = _cited(w, t, message_ids)
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=chat["id"], thread_ts=thread["ts"], kind="relay",
                     from_role="thread", from_session=caller.session_id, to_role="project-coordinator",
                     to_session=coordinator, via=["thread"], message_ids=[m["id"] for m in cited], note=text,
                     wake_id=f"deleg:{hop_id}")
        _handoff(w, coordinator, hop_id, envelope.relay(
            sender_role="thread", sender_session=caller.session_id, cited=cited, note=text, reason="relay",
            now=w.now, hop=hop_id, system_note=f"From thread “{thread['title']}” ({thread['ts']}). Answer it with "
                                               f"message_thread to {thread['ts']}."), "relay")
        return t.record(w, {"hop_id": hop_id, "delivered": True})
    return await chats.with_coordinator(chat, fn)


# ── threads ──────────────────────────────────────────────────────────────────

async def set_thread_resolved(t: ToolCall) -> dict[str, Any]:
    resolved = a.flag(t.args, "resolved")
    thread_id = a.text(t.args, "thread_id", required=False)

    def fn(w: Writes) -> dict[str, Any]:
        thread = _thread(w, t, thread_id)
        was = thread["state"] == "resolved"
        if was != resolved:
            rows.update_thread(w, thread["chat_id"], thread["ts"], state="resolved" if resolved else "working",
                               resolved_at=w.now if resolved else None)
        return {"thread_id": thread["ts"], "resolved": resolved, "transitioned": was != resolved}
    return await t.write(fn)


async def rename_thread(t: ToolCall) -> dict[str, Any]:
    title = a.text(t.args, "title", limit=TITLE_MAX)
    thread_id = a.text(t.args, "thread_id", required=False)

    def fn(w: Writes) -> dict[str, Any]:
        thread = _thread(w, t, thread_id)
        rows.update_thread(w, thread["chat_id"], thread["ts"], title=title)
        return {"thread_id": thread["ts"], "title": title}
    return await t.write(fn)


async def set_thread_label(t: ToolCall) -> dict[str, Any]:
    if "label" not in t.args:
        raise CommsError("missing_argument", "label is required (null clears it).")
    label = a.text(t.args, "label", required=False, limit=LABEL_MAX)
    thread_id = a.text(t.args, "thread_id", required=False)

    def fn(w: Writes) -> dict[str, Any]:
        thread = _thread(w, t, thread_id)
        rows.update_thread(w, thread["chat_id"], thread["ts"], label=label)
        return {"thread_id": thread["ts"], "label": label}
    return await t.write(fn)


# ── reading ──────────────────────────────────────────────────────────────────

async def list_projects(t: ToolCall) -> dict[str, Any]:
    names = await asyncio.to_thread(chats_pkg.project_folders)
    described = await asyncio.to_thread(lambda: {n: chats_pkg.describe_project(n) for n in names})

    def fn(w: Writes) -> dict[str, Any]:
        out = []
        for name in names:
            chat = w.execute("SELECT id FROM chats WHERE kind = 'project' AND project_name = ?", (name,)).fetchone()
            threads = [th for th in rows.list_threads(w, chat[0]) if th["state"] != "resolved"] if chat else []
            out.append({"project": name, "description": described[name],
                        "open_threads": [{"thread_id": th["ts"], "title": th["title"], "state": th["state"]}
                                         for th in threads[:OPEN_THREADS_LISTED]]})
        return {"projects": out}
    return await t.write(fn)


async def fetch_project_card(t: ToolCall) -> dict[str, Any]:
    name = a.text(t.args, "project")
    if name not in await asyncio.to_thread(chats_pkg.project_folders):
        raise CommsError("no_such_project", f"There is no project {name!r}. Use list_projects.")
    description = await asyncio.to_thread(chats_pkg.describe_project, name)

    def fn(w: Writes) -> dict[str, Any]:
        chat = w.execute("SELECT id FROM chats WHERE kind = 'project' AND project_name = ?", (name,)).fetchone()
        threads = rows.list_threads(w, chat[0], limit=THREADS_LISTED) if chat else []
        return {"project": name, "description": description, "threads": [_thread_row(w, th) for th in threads]}
    return await t.write(fn)


async def list_thread_sessions(t: ToolCall) -> dict[str, Any]:
    return await t.write(lambda w: {"threads": [_thread_row(w, th) for th in rows.list_threads(w, t.caller.chat_id)]})


async def fetch_thread(t: ToolCall) -> dict[str, Any]:
    thread_id = a.text(t.args, "thread_id", required=False)
    limit = a.limit(t.args, FETCH_THREAD)

    def fn(w: Writes) -> dict[str, Any]:
        thread = _thread(w, t, thread_id)
        root = rows.get_message(w, thread["ts"])
        recent = rows.latest_messages(w, chat_id=thread["chat_id"], thread_ts=thread["ts"], limit=limit)
        return {"thread_id": thread["ts"], "title": thread["title"], "label": thread["label"],
                "state": thread["state"], "status": thread["status_md"],
                "card": rows.latest_card(w, thread["chat_id"], thread["ts"]), "root": _brief(root) if root else None,
                "messages": [_brief(m) for m in recent]}
    return await t.write(fn)


async def fetch_timeline(t: ToolCall) -> dict[str, Any]:
    limit = a.limit(t.args, FETCH_TIMELINE)
    return await t.write(lambda w: {"messages": [_brief(m) for m in rows.latest_messages(
        w, chat_id=t.caller.chat_id, thread_ts=None, limit=limit)]})


async def fetch_messages(t: ToolCall) -> dict[str, Any]:
    """Exact messages by id, or a page of the timeline or a thread with a
    ``before`` / ``after`` message as the cursor (by seq)."""
    caller = t.caller
    message_ids = a.ids(t.args, "ids", limit=MAX_FETCH_IDS)
    thread_id = a.text(t.args, "thread_id", required=False)
    before = a.text(t.args, "before", required=False)
    after = a.text(t.args, "after", required=False)
    limit = a.limit(t.args, FETCH_TIMELINE)
    if message_ids and (thread_id or before or after):
        raise CommsError("invalid_argument", "Ask for ids, or for a page with thread_id, before or after; not both.")
    if before and after:
        raise CommsError("invalid_argument", "Page with before or with after, not both.")

    def fn(w: Writes) -> dict[str, Any]:
        if message_ids:
            return {"messages": [_brief(_message(w, t, m)) for m in message_ids], "has_more": False}
        thread = _thread(w, t, thread_id) if thread_id or caller.role == "thread" else None
        where = {"chat_id": caller.chat_id, "thread_ts": thread["ts"] if thread else None}
        if after:
            page = rows.page_messages(w, **where, timeline=thread is None, after_seq=_message(w, t, after)["seq"],
                                      limit=limit + 1)
            return {"messages": [_brief(m) for m in page[:limit]], "has_more": len(page) > limit}
        if before:
            page = rows.page_messages(w, **where, timeline=thread is None, before_seq=_message(w, t, before)["seq"],
                                      limit=limit + 1)
        else:
            page = rows.latest_messages(w, **where, limit=limit + 1)
        return {"messages": [_brief(m) for m in page[-limit:]], "has_more": len(page) > limit}
    return await t.write(fn)
