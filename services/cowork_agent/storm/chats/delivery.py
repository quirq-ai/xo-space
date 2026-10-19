"""Delivering the person's message (design ch 11 §2, §3, §7.4; sedc 09 #12).

* **In a thread:** straight to the thread's session (wake ``msg:<id>``,
  ``now``). A reply in a resolved thread reopens it, and one in a paused
  thread resumes it (§0.50): the thread goes back to ``working``.
* **On a timeline:** to the chat's router. ``agent``: the coordinator gets
  it. ``decision``: the decision model classifies it and routes it on the
  coordinator's behalf through the comms tools; whatever it doesn't route
  goes to the coordinator with a note saying why. ``off``: refused.

The message, its event and its wake are one transaction (ch 08 §3). A
retried POST with the same ``client_id`` stores one message and wakes no
one twice. Posting into a project or a thread skips the levels above it.
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
    if thread["state"] == "paused":
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

    def store(w: Writes) -> None:
        stored.update(rows.add_message(w, chat_id=chat["id"], author_kind="owner", author_id=OWNER, text=text,
                                       client_id=client_id))
    if routing["mode"] == router.DECISION:
        await engine.write(store)
        chats.spawn(_route_with_model(chats, chat, dict(stored), routing), f"route:{stored['id']}")
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


async def as_coordinator(chats: Chats, chat: Mapping[str, Any], tool: str, **arguments: Any) -> dict[str, Any]:
    """A comms call made on the coordinator's behalf (a decision model's
    routing, ch 11 §3.1): recorded as the coordinator's, through the same
    tool and its checks."""
    coordinator = await chats.with_coordinator(chat, lambda w, session_id: session_id)
    caller = await calls.caller_of(chats.engine, coordinator)
    return await calls.call(chats, caller, tool, arguments, HANDLERS)


async def _route_with_model(chats: Chats, chat: Mapping[str, Any], message: Mapping[str, Any],
                            routing: Mapping[str, Any]) -> None:
    """Classify the message with the chosen decision model and route it;
    anything it doesn't route goes to the coordinator, with why (D12)."""
    engine = chats.engine
    provider = routing["decision_model"]
    chosen = await engine.store.read(lambda conn: router.read_setting(conn, router.MODELS_KEY, {}))
    threads = await engine.store.read(lambda conn: rows.list_threads(conn, chat["id"]))
    names = await asyncio.to_thread(project_folders) if chat["kind"] == rows.WORKSPACE else []
    projects = await asyncio.to_thread(lambda: {n: describe_project(n) or n for n in names})
    route = await router.classify(provider=provider, model=(chosen.get(provider) or {}).get("model") or provider,
                                  kind=chat["kind"], text=message["text"], threads=threads, projects=projects,
                                  threshold=routing["abstain_below"])
    applied = False
    try:
        applied = await _apply(chats, chat, message, route)
    except calls.CommsError as exc:
        route["reasons"].append(f"could not apply: {exc.message}")
    if not applied:
        route["escalated_from"] = provider
    await engine.write(lambda w: router.record(w, chat_id=chat["id"], message_id=message["id"], decided_by=provider,
                                               route=route))
    if not applied:
        why = "; ".join(route["reasons"]) or "it routed nothing"
        await to_coordinator(chats, chat, message, note=f"The {provider} decision model handed this message to you "
                                                        f"({why}).")


async def _apply(chats: Chats, chat: Mapping[str, Any], message: Mapping[str, Any], route: Mapping[str, Any]) -> bool:
    actions = route["actions"]
    if not actions:
        return False
    provider = route["provider"]
    if chat["kind"] == rows.WORKSPACE:
        names = [a["project"] for a in actions]
        scope = (f"The owner's message concerns several projects ({', '.join(names)}); each got the same message. "
                 "Act only on the part that concerns this project.") if len(names) > 1 else None
        for name in names:
            await as_coordinator(chats, chat, "forward_to_project", project=name, context_message_ids=[message["id"]],
                                 **({"note": scope} if scope else {}))
        await as_coordinator(chats, chat, "post_message", text=f"Sent to {', '.join(names)}.")
        return True
    action = actions[0]
    if action["kind"] == "start_thread":
        await as_coordinator(chats, chat, "start_thread_session", message_id=message["id"],
                             title=router.title_from(message["text"]),
                             instructions=ROUTED_BRIEF.format(provider=provider), project_ack="On it, starting a thread.")
        return True
    thread = await chats.engine.store.read(lambda conn: rows.get_thread(conn, chat["id"], action["thread_id"]))
    await as_coordinator(chats, chat, "message_thread", thread_id=action["thread_id"],
                         context_message_ids=[message["id"]])
    await as_coordinator(chats, chat, "post_message",
                         text=f"Sent to [{thread['title'] if thread else 'its thread'}](#{action['thread_id']}).")
    return True
