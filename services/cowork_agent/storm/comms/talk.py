"""The tools agents speak, route and read with (design ch 12 §3–§5, §5.9;
the PoC's ``comms.py`` and ``workspace.py``).

Scope comes from the caller's session, never from an argument (ch 12 §6): a
coordinator acts on its own chat and that chat's threads; a thread on its
own thread (and reads its root message). Messages an agent cites are copied
by the server from the store with their true authors: any message of the
caller's own scope, the owner's, an agent's or XO's, and a cited owner message
brings the message it answered (``_cited``). What acts in the owner's name
(stopping a turn the owner started, moving a thread while its runtime can
work, a goal, a project) needs one of the owner's own messages among them
(``_owner_cited``). Every hop between agents is recorded with its wake in
the same transaction (ch 11 §16.2); its wake id is ``deleg:<hop id>``. A
task is made with its thread (``create_task``), traced to the founder's own
message it is anchored on, or else to the coordinator that asked (record
design v2 §1). ``to_coordinator`` and ``to_workspace`` are ``ask``'s hand-offs
(``owner.ask``).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Mapping

from services.cowork_agent.storm import chats as chats_pkg
from services.cowork_agent.storm.chats import attachments, envelope, rows, shared
from services.cowork_agent.storm.comms import args as a
from services.cowork_agent.storm.comms.calls import CitationRefused, CommsError, ToolCall
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
from services.cowork_agent.storm.engine import IntentRefused, Wake, WouldStopRuns, Writes
from services.cowork_agent.storm.presets import Intelligence
from services.cowork_agent.storm.records import FOUNDER, goals, ledger, tasks

log = logging.getLogger(__name__)

#: How a hop between agents queues its receiver's input (ch 07 §4.1).
HANDOFF_PRIORITY = "next"
#: How a coordinator's follow-up meets a thread's running turn: the wake's
#: ``delivery`` (None waits for the next turn; ``add`` joins the running one
#: where the runtime steers, else waits; ``now`` interrupts it first).
THREAD_DELIVERY = {"next": None, "steer": "add", "interrupt": "now"}
#: The thread-scoped event a coordinator's steer, interrupt or stop leaves.
CONTROL_EVENT = "thread.control"
#: Who a coordinator's stop is recorded as (``turn.stopped {by}``).
STOPPED_BY = "coordinator"
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
    """The messages an agent cites, copied from the store (ch 12 §6), each
    with its true author: any message of the caller's scope, the owner's, an
    agent's or XO's. Each cited owner message is preceded by the message it
    answered (``answered``), so an owner's bare "yes" reaches the receiver
    with the proposal it said yes to. An id that isn't there, or is outside
    the caller's scope, is refused (``CitationRefused``), which also tells
    the founder (plan W8)."""
    try:
        cited = [_message(w, t, m) for m in message_ids]
    except CommsError as exc:
        raise CitationRefused(exc.code, exc.message) from exc
    out: list[dict[str, Any]] = []
    seen = {m["id"] for m in cited}
    for message in cited:
        prior = answered(w, message) if message["author_kind"] == "owner" else None
        if prior is not None and prior["id"] not in seen and _in_scope(t, prior):
            seen.add(prior["id"])
            out.append(prior)
        out.append(message)
    return out


def answered(conn, message: Mapping[str, Any]) -> dict[str, Any] | None:
    """The message an owner's message answers: the one it replies to, else
    an agent's message right before it in the same place (the timeline, or
    the same thread), the proposal a short "yes" or "no" is about."""
    if message.get("reply_to"):
        return rows.get_message(conn, message["reply_to"])
    row = conn.execute(f"SELECT {rows.MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? AND thread_ts IS ? "
                       "AND seq < ? ORDER BY seq DESC LIMIT 1",
                       (message["chat_id"], message["thread_ts"], message["seq"])).fetchone()
    prior = rows.message_view(row) if row else None
    return prior if prior is not None and prior["author_kind"] == "agent" else None


def _owner_cited(cited: list[Mapping[str, Any]], action: str) -> list[Mapping[str, Any]]:
    """The owner's own messages among ``cited``, for what acts in the owner's
    name; none is a plain refusal (an agent's message is a fine citation,
    but it doesn't carry the owner's word)."""
    owners = [m for m in cited if m["author_kind"] == "owner"]
    if not owners:
        raise CommsError("owner_message_needed", f"{action} needs the owner's word: cite at least one of the owner's "
                                                 "own messages in context_message_ids (an agent's message can be "
                                                 "cited beside it, but doesn't carry the owner's authority), or ask "
                                                 "the owner first (ask, to \"founder\").")
    return owners


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
    agent = w.execute("SELECT agent FROM sessions WHERE id = ?", (thread["session_id"],)).fetchone()
    return {"thread_id": thread["ts"], "title": thread["title"], "label": thread["label"],
            "state": thread["activity"], "wait_reason": thread["wait_reason"],
            "waiting_until": thread["waiting_until"],
            "agent": agent[0] if agent else None, "headline": card.get("headline"),
            "needs_you": card.get("needs_you"), "last_activity_at": thread["updated_at"]}


def _handoff(w: Writes, session_id: str, hop_id: str, text: str, reason: str, images: tuple[str, ...] = (),
             delivery: str | None = None, confirmed_runs: int | None = None) -> None:
    """Queue a hop's wake for its receiver. One the founder's Stop refuses
    (``Writes.wake``) refuses the whole call (``IntentRefused``): the agent
    hears its message didn't go, rather than having it dropped."""
    w.wake(session_id, Wake(id=f"deleg:{hop_id}", text=text, priority="now" if delivery == "now" else HANDOFF_PRIORITY,
                            reason=reason, images=images, delivery=delivery, confirmed_runs=confirmed_runs))
    if w.refused is not None:
        raise IntentRefused(w.refused)


def _running_turn(conn, session_id: str) -> dict[str, Any] | None:
    """The session's running turn: its id, what started it and when."""
    row = conn.execute("SELECT id, origin, started_at FROM turns WHERE session_id = ? AND status = 'running' "
                       "ORDER BY started_at DESC LIMIT 1", (session_id,)).fetchone()
    return dict(zip(("id", "origin", "started_at"), row)) if row else None


def _owner_turn_guard(turn: Mapping[str, Any] | None, cited: list[Mapping[str, Any]], action: str) -> None:
    """The owner's word wins over a coordinator's: a turn the owner's own
    message started is interrupted or stopped only for a newer owner message."""
    if turn is None or turn["origin"] != "person":
        return
    if any(m["author_kind"] == "owner" and (m.get("created_at") or "") > turn["started_at"] for m in cited):
        return
    raise CommsError("owner_turn", f"The thread is working on the owner's own message. You can steer it "
                                   f"(delivery steer); {action} it only when you cite a newer owner message in "
                                   "context_message_ids.")


def _control(w: Writes, t: ToolCall, thread: Mapping[str, Any], *, action: str, reason: str | None,
             turn_id: str | None, say: str | None) -> None:
    """Record a coordinator's steer, interrupt or stop on the thread: a
    thread-scoped event, and (``say``) a line on the thread for the owner."""
    w.append(CONTROL_EVENT, {"action": action, "by": t.caller.role, "by_session": t.caller.session_id,
                             **({"reason": reason} if reason else {})},
             chat_id=t.caller.chat_id, session_id=thread["session_id"], thread_ts=thread["ts"], turn_id=turn_id)
    if say:
        rows.add_message(w, chat_id=t.caller.chat_id, thread_ts=thread["ts"], author_kind="system", author_id="xo",
                         text=say)


def _interrupted_this_turn(conn, session_id: str, turn_id: str) -> bool:
    return conn.execute("SELECT 1 FROM events WHERE session_id = ? AND turn_id = ? AND type = ? "
                        "AND json_extract(data, '$.action') = 'interrupt' LIMIT 1",
                        (session_id, turn_id, CONTROL_EVENT)).fetchone() is not None


def _images(folder: Path, messages: list[Mapping[str, Any]]) -> tuple[str, ...]:
    """The images the owner's ``messages`` carry, for the thread that works
    on them (a coordinator gets their paths only)."""
    return attachments.image_paths(folder, [f for m in messages for f in m["extra"].get("attachments") or ()])


# ── speaking ─────────────────────────────────────────────────────────────────

async def post_message(t: ToolCall) -> dict[str, Any]:
    text = a.text(t.args, "text")
    return await t.write(lambda w: {"message_id": rows.add_message(
        w, chat_id=t.caller.chat_id, author_kind="agent", author_id=t.caller.role,
        author_session_id=t.caller.session_id, text=text, extra=shared.extra_for(w, t.caller.session_id))["id"]})


async def reply(t: ToolCall) -> dict[str, Any]:
    text = a.text(t.args, "text")
    caller = t.caller

    def fn(w: Writes) -> dict[str, Any]:
        message = rows.add_message(w, chat_id=caller.chat_id, thread_ts=caller.thread_ts, author_kind="agent",
                                   author_id="thread", author_session_id=caller.session_id, text=text,
                                   extra=shared.extra_for(w, caller.session_id))     # what the turn shared
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
                                        "project's ask to the workspace.")
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


def founders_own(message: Mapping[str, Any]) -> bool:
    """Whether a stored message is the founder's own words in its chat: the
    owner's, and not a copy forwarded from another chat."""
    return message["author_kind"] == "owner" and not message["extra"].get("forwarded_from")


def _goal(w: Writes, t: ToolCall, goal_id: str) -> goals.Goal:
    """A live goal of the caller's chat."""
    goal = goals.get(w, goal_id)
    if goal is None or goal.chat_id != t.caller.chat_id:
        raise CommsError("no_such_goal", f"There is no goal {goal_id!r} in this chat.")
    if goal.state in goals.FINAL:
        raise CommsError("goal_ended", f"Goal {goal_id} is {goal.state}; set a new goal for new work.")
    return goal


async def create_task(t: ToolCall) -> dict[str, Any]:
    """A task and its thread, in one transaction: the thread's session, its
    brief hop and wake, and the task record (``instructions`` is the brief),
    anchored on a message of this chat's timeline. Anchored on the founder's
    own message, the task is the founder's, citing it; on any other (the
    coordinator's own post, a forwarded copy, a job's line) it is asked by
    the coordinator. A retried call returns the task the first one made and
    starts nothing."""
    chats, caller = t.chats, t.caller
    anchor_id = a.text(t.args, "message_id")
    title = a.text(t.args, "title", limit=TITLE_MAX)
    instructions = a.text(t.args, "instructions")
    extra_ids = [m for m in a.ids(t.args, "context_message_ids", limit=MAX_CITED) if m != anchor_id]
    ack = a.text(t.args, "ack", required=False, limit=LINE_MAX)
    project_ack = a.text(t.args, "project_ack", required=False, limit=SHORT_LINE_MAX)
    chosen = Intelligence(a.text(t.args, "model", required=False, limit=WORD_MAX),
                          a.text(t.args, "effort", required=False, limit=WORD_MAX))
    agent = a.text(t.args, "agent", required=False, limit=WORD_MAX)
    goal_id = a.text(t.args, "goal_id", required=False, limit=ID_MAX)
    key = ledger.request_key("create_task", caller.session_id, dict(t.args))
    chat = await chats.chat(caller.chat_id)

    done = await t.engine.store.read(lambda conn: tasks.by_key(conn, key))
    if done is not None:
        return await t.write(lambda w: _repeated(w, done))

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
        goal = _goal(w, t, goal_id) if goal_id else None
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
        task, _made = tasks.create(w, chat_id=chat["id"], thread_ts=anchor_id, title=title, instructions=instructions,
                                   asked_by=FOUNDER if founders_own(anchor) else caller.session_id,
                                   by=caller.session_id, request_key=key, goal_id=goal.id if goal else None)
        return t.record(w, {"task_id": task.id, "thread_id": anchor_id, "session_id": thread_session,
                            "hop_id": hop_id, **({"note": note} if note else {})}, task_id=task.id)
    # An agent always chooses the thread's model and effort (§0.105); a decision model may be unsure (D12).
    return await chats.start_thread(chat, anchor_id, title, chosen, also, must_choose=caller.decision is None,
                                    unchosen=caller.unchosen, agent=agent)


def _repeated(w: Writes, task: tasks.Task) -> dict[str, Any]:
    """What a retried ``create_task`` gets: the task the first call made."""
    thread = rows.get_thread(w, task.chat_id, task.thread_ts)
    return {"task_id": task.id, "thread_id": task.thread_ts, "session_id": thread["session_id"] if thread else None,
            "repeated": True}


async def message_thread(t: ToolCall) -> dict[str, Any]:
    """A follow-up to one of the coordinator's threads: it reopens a resolved
    thread, and answers the thread's own open question to it."""
    caller = t.caller
    thread_id = a.text(t.args, "thread_id")
    message_ids = a.ids(t.args, "context_message_ids", limit=MAX_CITED)
    note = a.text(t.args, "note", required=False)
    chosen = a.choice(t.args, "delivery", tuple(THREAD_DELIVERY)) or "next"
    confirm = _count(t.args, "confirm_stop_runs")
    if not message_ids and not note:
        raise CommsError("empty_relay", "Pass the owner's message in context_message_ids, a note, or both.")
    delivery = THREAD_DELIVERY[chosen]
    if delivery == "now":
        session_id = (await t.engine.store.read(lambda conn: _thread(conn, t, thread_id)))["session_id"]
        try:
            t.engine.refuses_now(session_id, confirm)
        except WouldStopRuns as exc:
            raise CommsError("would_stop_runs", f"Interrupting stops the thread's {exc.count} background "
                                                f"task(s) (sub-agents or shells). Call again with "
                                                f"confirm_stop_runs {exc.count} to go ahead, or use delivery "
                                                "steer.") from None

    def fn(w: Writes) -> dict[str, Any]:
        thread = _thread(w, t, thread_id)
        cited = _cited(w, t, message_ids)
        turn = _running_turn(w, thread["session_id"]) if delivery else None
        if delivery == "now" and turn is not None:
            _owner_turn_guard(turn, cited, "interrupt")
            if _interrupted_this_turn(w, thread["session_id"], turn["id"]):
                raise CommsError("already_interrupted", "You interrupted this turn already; steer it, or wait "
                                                        "for it to end (delivery next).")
        if delivery and turn is not None:
            past = "interrupted" if delivery == "now" else "steered"
            _control(w, t, thread, action=chosen, reason=None, turn_id=turn["id"],
                     say=f"The project coordinator {past} this thread's running turn." if delivery == "now" else None)
            log.info("storm comms: %s %s thread %s", caller.session_id, past, thread_id)
        reopened = thread["state"] == "resolved"
        task = tasks.of_thread(w, caller.chat_id, thread_id)
        if task is not None and task.state != "open":
            tasks.move(w, task, state="open", actor=caller.session_id)
        if reopened:
            rows.update_thread(w, caller.chat_id, thread_id, resolved_at=None)
        # The thread's open question to its coordinator (its ask; a relay from before the upgrade).
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
            now=w.now, hop=hop_id, folder=folder), kind, _images(folder, cited), delivery=delivery,
            confirmed_runs=confirm if delivery == "now" else None)
        return {"delivered": True, "thread_id": thread_id, "reopened": reopened, "hop_id": hop_id,
                **({"delivery": chosen, "turn_running": turn is not None} if delivery else {})}
    return await t.write(fn)


async def stop_thread(t: ToolCall) -> dict[str, Any]:
    """Stop one of the coordinator's threads: its running turn and sub-agents
    (``Engine.stop``); its session stays and resumes with its next input."""
    thread_id = a.text(t.args, "thread_id")
    reason = a.text(t.args, "reason", limit=WORD_MAX)
    message_ids = a.ids(t.args, "context_message_ids", limit=MAX_CITED)

    def check(conn) -> tuple[dict[str, Any], dict[str, Any] | None]:
        thread = _thread(conn, t, thread_id)
        turn = _running_turn(conn, thread["session_id"])
        _owner_turn_guard(turn, _cited(conn, t, message_ids), "stop")
        return thread, turn
    thread, turn = await t.engine.store.read(check)
    result = await t.engine.stop(thread["session_id"], by=STOPPED_BY)
    log.info("storm comms: %s stopped thread %s (%s)", t.caller.session_id, thread_id,
             "a turn" if result.stopped else "nothing running")

    def record(w: Writes) -> dict[str, Any]:
        task = tasks.of_thread(w, t.caller.chat_id, thread_id)
        if task is not None:
            tasks.move(w, task, state="stopped", actor=t.caller.session_id)
        _control(w, t, thread, action="stop", reason=reason, turn_id=result.turn_id or (turn or {}).get("id"),
                 say=f"The project coordinator stopped this thread: {reason}" if result.stopped else None)
        return {"thread_id": thread_id, "stopped": result.stopped}
    return await t.write(record)


async def continue_thread(t: ToolCall) -> dict[str, Any]:
    """Move a thread's work to a new linked thread (``chats.continuation``).
    On its own a coordinator may do it only while the thread's runtime can't
    take work; otherwise it cites the owner's message asking for it."""
    from services.cowork_agent.storm.chats import continuation

    caller = t.caller
    thread_id = a.text(t.args, "thread_id")
    reason = a.text(t.args, "reason", limit=WORD_MAX)
    agent = a.text(t.args, "agent", required=False, limit=WORD_MAX)
    model = a.text(t.args, "model", required=False, limit=WORD_MAX)
    effort = a.text(t.args, "effort", required=False, limit=WORD_MAX)
    instructions = a.text(t.args, "instructions", required=False)
    message_ids = a.ids(t.args, "context_message_ids", limit=MAX_CITED)
    chat = await t.chats.chat(caller.chat_id)

    def check(conn) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return _thread(conn, t, thread_id), _cited(conn, t, message_ids)
    thread, cited = await t.engine.store.read(check)
    if not any(m["author_kind"] == "owner" for m in cited):
        health = await t.engine.health((await t.engine.meta(thread["session_id"])).agent)
        if health.usable:
            raise CommsError("owner_must_ask", "The thread's runtime can take work now, so move it only when the "
                                               "owner asks: cite their message in context_message_ids, or "
                                               "ask the owner first (ask, to \"founder\").")
    result = await continuation.continue_thread(t.chats, chat, thread["ts"], agent=agent, model=model, effort=effort,
                                                reason=reason, instructions=instructions, by=caller.role)
    return await t.write(lambda w: result)


def _count(args: Mapping[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CommsError("invalid_argument", f"{key} is a whole number from 1.")
    return value


async def to_workspace(t: ToolCall) -> dict[str, Any]:
    """``ask`` to the workspace: a project coordinator asks the workspace
    coordinator, maybe on a thread's behalf; the answer comes back as a
    ``forward_to_project`` note (an ``answer`` hop)."""
    chats, caller = t.chats, t.caller
    question = a.text(t.args, "question")
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
                            "note": "The answer will arrive as a relay. End your turn now."}, to="workspace")
    return await chats.with_coordinator(workspace, fn)


async def to_coordinator(t: ToolCall) -> dict[str, Any]:
    """``ask`` to the coordinator: a thread hands a question or work to its
    own project coordinator, which it wakes; it can name no one else (ch 12
    §5.1). Progress goes to ``report`` instead, which wakes no one."""
    chats, caller = t.chats, t.caller
    question = a.text(t.args, "question")
    message_ids = a.ids(t.args, "context_message_ids", limit=MAX_CITED)
    chat = await chats.chat(caller.chat_id)

    def fn(w: Writes, coordinator: str) -> dict[str, Any]:
        thread = _thread(w, t, None)
        cited = _cited(w, t, message_ids)
        hop_id = rows.new_id("hop")
        rows.add_hop(w, hop_id=hop_id, chat_id=chat["id"], thread_ts=thread["ts"], kind="ask",
                     from_role="thread", from_session=caller.session_id, to_role="project-coordinator",
                     to_session=coordinator, via=["thread"], message_ids=[m["id"] for m in cited], note=question,
                     wake_id=f"deleg:{hop_id}")
        _handoff(w, coordinator, hop_id, envelope.relay(
            sender_role="thread", sender_session=caller.session_id, cited=cited, note=question, reason="ask",
            now=w.now, hop=hop_id, system_note=f"From thread “{thread['title']}” ({thread['ts']}). Answer it with "
                                               f"message_thread to {thread['ts']}."), "ask")
        return t.record(w, {"hop_id": hop_id, "delivered": True,
                            "note": "Your coordinator is woken; its answer arrives as a relay."}, to="coordinator")
    return await chats.with_coordinator(chat, fn)


# ── threads ──────────────────────────────────────────────────────────────────

async def set_thread_resolved(t: ToolCall) -> dict[str, Any]:
    resolved = a.flag(t.args, "resolved")
    thread_id = a.text(t.args, "thread_id", required=False)

    def fn(w: Writes) -> dict[str, Any]:
        thread = _thread(w, t, thread_id)
        was = thread["state"] == "resolved"
        if was != resolved:
            rows.update_thread(w, thread["chat_id"], thread["ts"], resolved_at=w.now if resolved else None)
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
                        "open_threads": [{"thread_id": th["ts"], "title": th["title"], "state": th["activity"]}
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
                "state": thread["activity"], "wait_reason": thread["wait_reason"],
                "waiting_until": thread["waiting_until"], "status": thread["status_md"],
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
