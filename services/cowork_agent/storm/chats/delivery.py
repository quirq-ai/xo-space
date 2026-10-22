"""Delivering the person's message (design ch 11 §2, §3, §7.4; sedc 09 #12).

* **In a thread:** straight to the thread's session (wake ``msg:<id>``,
  ``now``). A reply in a resolved thread reopens it, and one in a paused
  thread resumes it (§0.50): the thread goes back to ``working``.
* **On a timeline:** to the chat's router. ``agent``: the coordinator gets
  it. ``decision``: the decision model classifies it and routes it on the
  coordinator's behalf through the comms tools; whatever it doesn't route
  goes to the coordinator with a note saying why. ``off``: refused.

The message, its event and its wake are one transaction (ch 08 §3); with a
decision model, the wake's place is taken by a pending ``router_decisions``
row, settled when the model's routing is done, and handed to the
coordinator at boot if a restart cut it short. A retried POST with the same
``client_id`` stores one message and routes it once. Posting into a project
or a thread skips the levels above it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from services.cowork_agent.storm.chats import ChatRefused, Chats, describe_project, project_folders, router, rows
from services.cowork_agent.storm.chats import envelope
from services.cowork_agent.storm.comms import calls
from services.cowork_agent.storm.comms.handlers import HANDLERS
from services.cowork_agent.storm.engine import SessionRefused, Wake, Writes

log = logging.getLogger(__name__)

OWNER = "owner"
#: What the timeline says when a message reached no one.
UNDELIVERED = "This message reached no one: {why}. Send it again once that is fixed."
#: What a decision model sends a thread it starts (ch 11 §7.1).
ROUTED_BRIEF = "Routed by {provider}: the owner's message is attached; do what it asks."


def _owner_wake(chat: Mapping[str, Any], message: Mapping[str, Any], *, note: str | None = None):
    """The wake for the person's message, built in the transaction that
    stored it (the envelope quotes the stored row)."""
    def build(w: Writes) -> Wake:
        return Wake(id=f"msg:{message['id']}", priority="now", reason="message", origin="person",
                    text=envelope.wake(reason="mention", chat=chat, trigger=message, now=w.now,
                                       thread_ts=message["thread_ts"], system_note=note))
    return build


async def post(chats: Chats, chat_id: str, *, text: str, thread_ts: str | None, client_id: str) -> dict[str, Any]:
    chat = await chats.chat(chat_id)
    chats.writable(chat)
    if thread_ts:
        return await _to_thread(chats, chat, text=text, ts=thread_ts, client_id=client_id)
    return await _to_timeline(chats, chat, text=text, client_id=client_id)


async def _to_thread(chats: Chats, chat: Mapping[str, Any], *, text: str, ts: str, client_id: str) -> dict[str, Any]:
    engine = chats.engine
    thread = await engine.store.read(lambda conn: rows.get_thread(conn, chat["id"], ts))
    if thread is None:
        raise ChatRefused("thread_not_found", f"There is no thread {ts!r} in this chat.", status=404)
    stored: dict[str, Any] = {}

    def store(w: Writes) -> None:
        stored.update(rows.add_message(w, chat_id=chat["id"], thread_ts=ts, author_kind="owner", author_id=OWNER,
                                       text=text, client_id=client_id))
        if rows.get_thread(w, chat["id"], ts)["state"] in ("resolved", "paused"):
            rows.update_thread(w, chat["id"], ts, state="working", resolved_at=None)
    await engine.deliver(thread["session_id"], lambda w: _owner_wake(chat, stored)(w), also=store)
    if (await engine.get_session(thread["session_id"]))["paused"]:
        await engine.resume(thread["session_id"])
    return stored


async def _to_timeline(chats: Chats, chat: Mapping[str, Any], *, text: str, client_id: str) -> dict[str, Any]:
    engine = chats.engine

    def known(conn) -> tuple[dict[str, Any], dict[str, Any] | None]:
        row = conn.execute(f"SELECT {rows.MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? AND client_id = ?",
                           (chat["id"], client_id)).fetchone()
        return router.effective(conn, chat["id"]), rows.message_view(row) if row else None
    routing, retried = await engine.store.read(known)
    if retried is not None:
        return retried                          # stored and routed the first time
    if routing["mode"] == router.OFF:
        raise router.RouterRefused("router_off", "The router is off, so this chat takes no new messages. Turn it on "
                                                 "with PUT /api/chat/router.", status=409)
    stored: dict[str, Any] = {}
    pending: list[str] = []

    def store(w: Writes) -> None:
        row = w.execute(f"SELECT {rows.MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? AND client_id = ?",
                        (chat["id"], client_id)).fetchone()
        if row is not None:                     # an overlapping retry stored it first, and routes it
            stored.update(rows.message_view(row))
            return
        stored.update(rows.add_message(w, chat_id=chat["id"], author_kind="owner", author_id=OWNER, text=text,
                                       client_id=client_id))
        if routing["mode"] == router.DECISION:
            pending.append(router.record(w, chat_id=chat["id"], message_id=stored["id"],
                                         decided_by=routing["decision_model"], route=router.PENDING))
    if routing["mode"] == router.DECISION:
        await engine.write(store)
        if pending:
            chats.spawn(_route_with_model(chats, chat, dict(stored), routing, pending[0]), f"route:{stored['id']}")
        return stored
    await to_coordinator(chats, chat, stored, also=store)
    return stored


async def to_coordinator(chats: Chats, chat: Mapping[str, Any], message: Mapping[str, Any], *,
                         also=None, note: str | None = None) -> None:
    """Wake the chat's coordinator with the person's message (stored by
    ``also`` in the same transaction, or already). A coordinator retired
    meanwhile hands it to its successor."""
    wake = _owner_wake(chat, message, note=note)
    for _attempt in range(2):
        coordinator = await chats.with_coordinator(chat, lambda w, session_id: session_id)
        try:
            await chats.engine.deliver(coordinator, lambda w: wake(w), also=also)
            return
        except SessionRefused as exc:
            if exc.code != "session_retired":
                raise
    raise ChatRefused("coordinator_busy", "This chat's coordinator was just replaced; send the message again.",
                      status=409)


async def as_coordinator(chats: Chats, chat: Mapping[str, Any], decision: str, tool: str,
                         **arguments: Any) -> dict[str, Any]:
    """A comms call made on the coordinator's behalf by the router decision
    ``decision`` (a decision model's routing, ch 11 §3.1): recorded as the
    coordinator's, through the same tool and its checks; its hops point to
    the decision (§16.2)."""
    coordinator = await chats.with_coordinator(chat, lambda w, session_id: session_id)
    caller = await calls.caller_of(chats.engine, coordinator, decision=decision)
    return await calls.call(chats, caller, tool, arguments, HANDLERS)


async def recover(chats: Chats) -> None:
    """At boot: a decision model's routing a restart cut short goes to the
    coordinator, with what it had done already (its hops point to it)."""
    engine = chats.engine
    cut = await engine.store.read(lambda conn: conn.execute(
        "SELECT d.id, d.chat_id, d.decided_by, d.message_id, (SELECT count(*) FROM hops h WHERE h.cause = d.id) "
        "FROM router_decisions d WHERE json_extract(d.route, '$.pending') ORDER BY d.created_at").fetchall())
    for decision, chat_id, provider, message_id, hops in cut:
        try:
            chat = await chats.chat(chat_id)
            message = await engine.store.read(lambda conn: rows.get_message(conn, message_id))
        except Exception:  # noqa: BLE001 - one chat's failure must not keep the others' messages back
            log.exception("storm chats: message %s, cut short by a restart, can't be read", message_id)
            continue
        route = {"provider": provider, "actions": [], "escalated_from": provider,
                 "reasons": ["the server restarted while it routed this message"]}
        done = (f"; it had already made {hops} hand-off(s), listed in this chat's hops with cause {decision}, so "
                "check them before acting again") if hops else ""
        await hand_over(chats, chat, message, decision, route,
                        note=f"The server restarted while the {provider} decision model was routing this "
                             f"message{done}.")


async def _route_with_model(chats: Chats, chat: Mapping[str, Any], message: Mapping[str, Any],
                            routing: Mapping[str, Any], decision: str) -> None:
    """Classify the message with the chosen decision model and route it;
    anything it doesn't route goes to the coordinator, with why (D12). The
    pending ``decision`` is settled with the route, in the transaction that
    wakes the coordinator when it routed nothing."""
    engine = chats.engine
    provider = routing["decision_model"]
    try:
        chosen = await engine.store.read(lambda conn: router.read_setting(conn, router.MODELS_KEY, {}))
        threads = await engine.store.read(lambda conn: rows.list_threads(conn, chat["id"]))
        names = await asyncio.to_thread(project_folders) if chat["kind"] == rows.WORKSPACE else []
        projects = await asyncio.to_thread(lambda: {n: describe_project(n) or n for n in names})
        route = await router.classify(provider=provider, model=(chosen.get(provider) or {}).get("model") or provider,
                                      kind=chat["kind"], text=message["text"], threads=threads, projects=projects,
                                      threshold=routing["abstain_below"])
    except Exception as exc:  # noqa: BLE001 - whatever failed, the coordinator still gets the message
        log.exception("storm chats: the %s decision model failed on message %s", provider, message["id"])
        route = {"provider": provider, "actions": [], "reasons": [f"it failed ({type(exc).__name__})"]}
    applied = False
    try:
        applied = await _apply(chats, chat, message, route, decision)
    except Exception as exc:  # noqa: BLE001 - what it didn't apply goes to the coordinator
        if not isinstance(exc, calls.CommsError):
            log.exception("storm chats: applying the %s decision on message %s failed", provider, message["id"])
        route["reasons"].append(f"could not apply: {getattr(exc, 'message', None) or type(exc).__name__}")
    if applied:
        await engine.write(lambda w: router.settle(w, decision, route))
        return
    route["escalated_from"] = provider
    why = "; ".join(route["reasons"]) or "it routed nothing"
    await hand_over(chats, chat, message, decision, route,
                    note=f"The {provider} decision model handed this message to you ({why}).")


async def hand_over(chats: Chats, chat: Mapping[str, Any], message: Mapping[str, Any], decision: str,
                    route: dict[str, Any], *, note: str) -> None:
    """Settle the decision and wake the coordinator with the message, in one
    transaction. When the coordinator can't take it (it can't start), the
    decision says so and the timeline tells the person (the POST answered
    long ago)."""
    try:
        await to_coordinator(chats, chat, message, also=lambda w: router.settle(w, decision, route), note=note)
    except Exception as exc:  # noqa: BLE001 - the person must hear that the message reached no one
        log.exception("storm chats: handing message %s to its coordinator failed", message["id"])
        why = getattr(exc, "message", None) or type(exc).__name__
        route["reasons"].append(f"the coordinator could not take it: {why}")

        def say(w: Writes) -> None:
            router.settle(w, decision, route)
            rows.add_message(w, chat_id=chat["id"], author_kind="system", author_id="xo",
                             text=UNDELIVERED.format(why=why.rstrip(".")), reply_to=message["id"])
        await chats.engine.write(say)


async def _apply(chats: Chats, chat: Mapping[str, Any], message: Mapping[str, Any], route: Mapping[str, Any],
                 decision: str) -> bool:
    actions = route["actions"]
    if not actions:
        return False
    provider = route["provider"]
    if chat["kind"] == rows.WORKSPACE:
        names = [a["project"] for a in actions]
        scope = (f"The owner's message concerns several projects ({', '.join(names)}); each got the same message. "
                 "Act only on the part that concerns this project.") if len(names) > 1 else None
        for name in names:
            await as_coordinator(chats, chat, decision, "forward_to_project", project=name,
                                 context_message_ids=[message["id"]], **({"note": scope} if scope else {}))
        await as_coordinator(chats, chat, decision, "post_message", text=f"Sent to {', '.join(names)}.")
        return True
    action = actions[0]
    if action["kind"] == "start_thread":
        await as_coordinator(chats, chat, decision, "start_thread_session", message_id=message["id"],
                             title=router.title_from(message["text"]),
                             instructions=ROUTED_BRIEF.format(provider=provider), project_ack="On it, starting a thread.")
        return True
    thread = await chats.engine.store.read(lambda conn: rows.get_thread(conn, chat["id"], action["thread_id"]))
    await as_coordinator(chats, chat, decision, "message_thread", thread_id=action["thread_id"],
                         context_message_ids=[message["id"]])
    await as_coordinator(chats, chat, decision, "post_message",
                         text=f"Sent to [{thread['title'] if thread else 'its thread'}](#{action['thread_id']}).")
    return True
