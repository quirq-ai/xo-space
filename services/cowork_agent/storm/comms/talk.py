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
import logging
from pathlib import Path
from typing import Any, Mapping

from services.cowork_agent.storm import chats as chats_pkg
from services.cowork_agent.storm.chats import attachments, envelope, rows
from services.cowork_agent.storm.comms import args as a
from services.cowork_agent.storm.comms.calls import CommsError, ToolCall
from services.cowork_agent.storm.comms.tools import (
    EMOJI_MAX,
    ID_MAX,
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

log = logging.getLogger(__name__)

#: How a hop between agents queues its receiver's input (ch 07 §4.1).
HANDOFF_PRIORITY = "next"
FETCH_TIMELINE = 30
FETCH_THREAD = 20
OPEN_THREADS_LISTED = 10
THREADS_LISTED = 20


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


def _handoff(w: Writes, session_id: str, hop_id: str, text: str, reason: str, images: tuple[str, ...] = ()) -> None:
    w.wake(session_id, Wake(id=f"deleg:{hop_id}", text=text, priority=HANDOFF_PRIORITY, reason=reason, images=images))


def _images(folder: Path, messages: list[Mapping[str, Any]]) -> tuple[str, ...]:
    """The images the owner's ``messages`` carry, for the thread that works
    on them (a coordinator gets their paths only)."""
    return attachments.image_paths(folder, [f for m in messages for f in m["extra"].get("attachments") or ()])


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
    (the hop is an ``answer`` to the ``ask``, ch 12 §4).

    With ``ask_id``, the forward serves that ask: addressed to the project
    that asked, it answers it; addressed to another project, it hands the
    asking project's work on, citing the owner's messages the ask carried,
    and the asking project is told where it went (``_tell_asker``)."""
    chats = t.chats
    name = a.text(t.args, "project")
    message_ids = a.ids(t.args, "context_message_ids", limit=MAX_CITED)
    note = a.text(t.args, "note", required=False)
    ask_id = a.text(t.args, "ask_id", required=False, limit=ID_MAX)
    if not message_ids and not note:
        raise CommsError("empty_forward", "Pass the owner's message ids in context_message_ids, a note, or both.")
    if name not in await asyncio.to_thread(chats_pkg.project_folders):
        raise CommsError("no_such_project", f"There is no project {name!r}. Use list_projects, or propose_project "
                                            "for new work.")
    target = await chats.project(name)
    workspace_session = t.caller.session_id
    passed: dict[str, Any] = {}

    def originals_of(w: Writes) -> tuple[dict[str, Any] | None, bool, list[dict[str, Any]]]:
        ask = _ask(w, ask_id) if ask_id else None
        onward = ask is not None and ask["chat_id"] != target["id"]
        return ask, onward, _carried(w, ask, message_ids) if onward else _cited(w, t, message_ids)
    # The files go first, outside the transaction: checked like the call itself,
    # then copied into the project, where its sessions read them.
    carried = await attachments.carry(chats, (await t.engine.store.read(originals_of))[2], target)

    def fn(w: Writes, coordinator: str) -> dict[str, Any]:
        ask, onward, originals = originals_of(w)
        via = [_ask_project(w, ask), "workspace"] if onward else ["workspace"]
        copies = [rows.add_message(w, chat_id=target["id"], author_kind="owner", author_id=m["author_id"],
                                   text=m["text"], extra={"forwarded_from": {"chat_id": m["chat_id"],
                                                                             "message_id": m["id"],
                                                                             "via": via},
                                                          **({"attachments": carried[m["id"]]}
                                                             if carried.get(m["id"]) else {})})
                  for m in originals]
        if ask is not None:
            question = None if onward else ask
        else:
            question = None if copies else _open_hop(w, chat_id=target["id"], kind="ask",
                                                     to_role="workspace-coordinator")
        kind = "forward" if copies or onward else "answer" if question else "relay"
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=target["id"], thread_ts=question["thread_ts"] if question else None,
                     kind=kind, from_role="workspace-coordinator", from_session=workspace_session,
                     to_role="project-coordinator", to_session=coordinator,
                     via=["project", "workspace"] if onward else ["workspace"],
                     cause=t.caller.decision or (ask["id"] if ask else originals[0]["id"] if originals else
                                                 question["id"] if question else None),
                     message_ids=[m["id"] for m in copies], note=note, wake_id=f"deleg:{hop_id}")
        if question:
            rows.set_hop_state(w, question["id"], "answered")
        parts = [envelope.relay(sender_role="workspace-coordinator", sender_session=workspace_session, note=note,
                                reason=kind, now=w.now, hop=hop_id)] if note else []
        parts += [envelope.wake(reason="mention", chat=target, trigger=copy, now=w.now) for copy in copies]
        _handoff(w, coordinator, hop_id, "\n\n".join(parts), kind)
        if onward:
            passed.update(ask=ask, hop_id=hop_id, copies=[m["id"] for m in copies])
        return t.record(w, {"project": name, "hop_id": hop_id, "forwarded_message_ids": [m["id"] for m in copies]})
    result = await chats.with_coordinator(target, fn)
    if passed:
        await _tell_asker(t, passed["ask"], project=name, forward=passed["hop_id"], copies=passed["copies"])
    return result


def _ask(w: Writes, hop_id: str) -> dict[str, Any]:
    """The open ``ask`` a forward serves: a project coordinator's question
    to the workspace (any incarnation of it: a successor answers its
    predecessor's asks)."""
    row = w.execute(f"SELECT {rows.HOP_COLUMNS} FROM hops WHERE id = ?", (hop_id,)).fetchone()
    ask = rows.hop_view(row) if row else None
    if ask is None or ask["kind"] != "ask" or ask["to_role"] != "workspace-coordinator":
        raise CommsError("no_such_ask", f"There is no ask {hop_id!r} to the workspace. ask_id is the hop id of a "
                                        "project's ask_workspace relay.")
    if ask["state"] == "answered":
        raise CommsError("ask_answered", f"The ask {hop_id!r} has already been answered or passed on.")
    return ask


def _ask_project(w: Writes, ask: Mapping[str, Any]) -> str:
    chat = rows.get_chat(w, ask["chat_id"])
    return (chat or {}).get("project") or "project"


def _carried(w: Writes, ask: Mapping[str, Any], message_ids: list[str]) -> list[dict[str, Any]]:
    """The owner's messages an ask carried, the only ones of another chat
    the workspace may cite (checked against the asking chat's scope when
    the ask was made)."""
    carried = set(ask["message_ids"])
    outside = [m for m in message_ids if m not in carried]
    if outside:
        raise CommsError("no_such_message", f"{outside[0]!r} isn't one of the owner's messages the ask carried "
                                            f"({', '.join(ask['message_ids']) or 'none'}).")
    found = [rows.get_message(w, m) for m in message_ids]
    if any(m is None for m in found):
        raise CommsError("no_such_message", "A message the ask carried no longer exists.")
    return found


#: What the asking project's timeline says when its ask went to another project.
PASSED_ON = "Passed to {project} through the Workspace."


async def _tell_asker(t: ToolCall, ask: Mapping[str, Any], *, project: str, forward: str, copies: list[str]) -> None:
    """The asking project hears where its ask went, in one transaction: an
    ``answer`` hop to its coordinator (cause: the forward), the ask marked
    answered, and a line on its timeline written by XO, so the owner sees
    the hop whoever reads it. After the forward's own transaction: if this
    fails, the work has still reached the project, and the ask stays open."""
    chats = t.chats
    try:
        asker = await chats.chat(ask["chat_id"])

        def fn(w: Writes, coordinator: str) -> None:
            hop_id = rows.new_id("hop")
            note = f"Passed to project “{project}”."
            rows.add_hop(w, hop_id=hop_id, chat_id=asker["id"], thread_ts=ask["thread_ts"], kind="answer",
                         from_role="workspace-coordinator", from_session=t.caller.session_id,
                         to_role="project-coordinator", to_session=coordinator, via=["workspace"], cause=forward,
                         note=note, wake_id=f"deleg:{hop_id}")
            rows.set_hop_state(w, ask["id"], "answered")
            rows.add_message(w, chat_id=asker["id"], author_kind="system", author_id="xo",
                             text=PASSED_ON.format(project=project),
                             extra={"passed_on": {"project": project, "ask_hop_id": ask["id"],
                                                  "forward_hop_id": forward, "forwarded_message_ids": copies}})
            who = " Tell the thread that asked you, if it waits on this." if ask["thread_ts"] else ""
            _handoff(w, coordinator, hop_id, envelope.relay(
                sender_role="xo", sender_session=None, reason="answer", now=w.now, hop=hop_id,
                system_note=f"The workspace passed your ask ({ask['id']}) to project “{project}”. XO has already "
                            f"said so on your timeline.{who} Otherwise end your turn with no_reply_needed."),
                "answer")
        await chats.with_coordinator(asker, fn)
    except Exception:  # noqa: BLE001 - the forward is done; only the asker's notice failed
        log.exception("storm comms: telling chat %s that its ask %s was passed on failed", ask["chat_id"], ask["id"])


def _open_hop(w: Writes, *, chat_id: str, kind: str | tuple[str, ...], to_role: str, from_session: str | None = None,
              thread_ts: str | None = None) -> dict[str, Any] | None:
    """The latest hop of ``kind`` (one, or any of several) to ``to_role``
    that hasn't been answered."""
    kinds = (kind,) if isinstance(kind, str) else kind
    marks = ", ".join("?" * len(kinds))
    row = w.execute(f"SELECT {rows.HOP_COLUMNS} FROM hops WHERE chat_id = ? AND kind IN ({marks}) AND to_role = ? "
                    "AND state <> 'answered' AND (? IS NULL OR from_session = ?) AND (? IS NULL OR thread_ts = ?) "
                    "ORDER BY seq DESC LIMIT 1",
                    (chat_id, *kinds, to_role, from_session, from_session, thread_ts, thread_ts)).fetchone()
    return rows.hop_view(row) if row else None


async def start_thread_session(t: ToolCall) -> dict[str, Any]:
    chats, caller = t.chats, t.caller
    anchor_id = a.text(t.args, "message_id")
    title = a.text(t.args, "title", limit=TITLE_MAX)
    instructions = a.text(t.args, "instructions")
    extra_ids = [m for m in a.ids(t.args, "context_message_ids", limit=MAX_CITED) if m != anchor_id]
    ack = a.text(t.args, "ack", required=False, limit=LINE_MAX)
    project_ack = a.text(t.args, "project_ack", required=False, limit=SHORT_LINE_MAX)
    chosen = Intelligence(a.text(t.args, "model", required=False, limit=WORD_MAX),
                          a.text(t.args, "effort", required=False, limit=WORD_MAX))
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

    def also(w: Writes, thread_session: str, note: str | None) -> dict[str, Any]:
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
                     to_session=thread_session, via=via, cause=caller.decision or anchor_id,
                     message_ids=[anchor_id, *(m["id"] for m in cited)], note=instructions,
                     wake_id=f"deleg:{hop_id}")
        # One wake, so the brief and the owner's message reach the thread in one turn (D34).
        folder = attachments.chat_folder(chat)
        brief = envelope.relay(sender_role=caller.role, sender_session=caller.session_id, cited=cited,
                               note=instructions, reason="spawn", now=w.now, hop=hop_id, folder=folder)
        first = envelope.wake(reason="mention", chat=chat, trigger=anchor, now=w.now, role="initiator")
        _handoff(w, thread_session, hop_id, f"{brief}\n\n{first}", "spawn", _images(folder, [anchor, *cited]))
        return t.record(w, {"thread_id": anchor_id, "session_id": thread_session, "hop_id": hop_id,
                            **({"note": note} if note else {})})
    # An agent always chooses the thread's model and effort (§0.105); a decision model may be unsure (D12).
    return await chats.start_thread(chat, anchor_id, title, chosen, also, must_choose=caller.decision is None,
                                    unchosen=caller.unchosen)


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
        # The thread's open question: its message_coordinator, or its ask to the owner (ch 15 §5.2).
        question = _open_hop(w, chat_id=caller.chat_id, kind=("relay", "ask"), to_role="project-coordinator",
                             from_session=thread["session_id"], thread_ts=thread_id)
        kind = "answer" if question else "relay"
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=caller.chat_id, thread_ts=thread_id, kind=kind,
                     from_role=caller.role, from_session=caller.session_id, to_role="thread",
                     to_session=thread["session_id"], via=["project"],
                     cause=caller.decision or (question["id"] if question else (cited[0]["id"] if cited else None)),
                     message_ids=[m["id"] for m in cited], note=note, wake_id=f"deleg:{hop_id}")
        if question:
            rows.set_hop_state(w, question["id"], "answered")
        folder = attachments.chat_folder(rows.get_chat(w, caller.chat_id))
        _handoff(w, thread["session_id"], hop_id, envelope.relay(
            sender_role=caller.role, sender_session=caller.session_id, cited=cited, note=note, reason=kind,
            now=w.now, hop=hop_id, folder=folder), kind, _images(folder, cited))
        return {"delivered": True, "thread_id": thread_id, "reopened": reopened, "hop_id": hop_id}
    return await t.write(fn)


async def ask_workspace(t: ToolCall) -> dict[str, Any]:
    """A project coordinator asks the workspace coordinator, maybe on a
    thread's behalf; the answer comes back as a ``forward_to_project``
    note (an ``answer`` hop)."""
    chats, caller = t.chats, t.caller
    question = a.text(t.args, "question", limit=LONG_TEXT_MAX)
    on_behalf = a.text(t.args, "on_behalf_of", required=False)
    message_ids = a.ids(t.args, "context_message_ids", limit=MAX_CITED)
    project = await chats.chat(caller.chat_id)
    workspace = await chats.workspace()

    def fn(w: Writes, coordinator: str) -> dict[str, Any]:
        thread = _thread(w, t, on_behalf) if on_behalf else None
        cited = _cited(w, t, message_ids)
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=project["id"], thread_ts=on_behalf, kind="ask",
                     from_role=caller.role, from_session=caller.session_id, to_role="workspace-coordinator",
                     to_session=coordinator, via=["thread", "project"] if thread else ["project"],
                     message_ids=[m["id"] for m in cited], note=question, wake_id=f"deleg:{hop_id}")
        who = f" on behalf of its thread “{thread['title']}”" if thread else ""
        name = project["project"]
        carried = (f" To hand it to another project, call forward_to_project to that project with ask_id "
                   f"“{hop_id}” and the cited messages' ids in context_message_ids ({', '.join(m['id'] for m in cited)}"
                   f"); XO then tells “{name}” where it went.") if cited else ""
        _handoff(w, coordinator, hop_id, envelope.relay(
            sender_role=caller.role, sender_session=caller.session_id, cited=cited, note=question, reason="ask",
            now=w.now, hop=hop_id, folder=attachments.chat_folder(project),
            system_note=f"The coordinator of project “{name}” asks this{who}. Answer it with forward_to_project to "
                        f"“{name}”, a note only, with ask_id “{hop_id}”.{carried}"), "ask")
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
