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
from services.cowork_agent.storm.engine import Engine, SessionRefused, Wake, Writes
from services.cowork_agent.storm.engine.catalog import Catalog

log = logging.getLogger(__name__)

OWNER = "owner"
#: What the timeline says when a message reached no one.
UNDELIVERED = "This message reached no one: {why}. Send it again once that is fixed."
#: What a decision model sends a thread it starts (ch 11 §7.1).
ROUTED_BRIEF = "Routed by {provider}: the owner's message is attached; do what it asks."


def _owner_wake(chat: Mapping[str, Any], message: Mapping[str, Any], *, typed: str | None = None,
                note: str | None = None):
    """The wake for the person's message, built in the transaction that
    stored it. The envelope quotes the stored row with the text as the person
    ``typed`` it, when known (§0.112): the session they sent it to gets their
    words, secrets included, and once the wake is consumed or dropped its
    envelope becomes the one quoting the stored, redacted row (``kept``).
    Relays and citations quote the stored row, so a secret reaches only that
    session."""
    def build(w: Writes) -> Wake:
        def quoting(row: Mapping[str, Any]) -> str:
            return envelope.wake(reason="mention", chat=chat, trigger=row, now=w.now,
                                 thread_ts=message["thread_ts"], system_note=note)
        kept = quoting(message)
        text = quoting({**message, "text": typed}) if typed is not None else kept
        return Wake(id=_wake_id(message), priority="now", reason="message", origin="person", text=text, kept=kept)
    return build


def _wake_id(message: Mapping[str, Any]) -> str:
    return f"msg:{message['id']}"


async def post(chats: Chats, chat_id: str, *, text: str, thread_ts: str | None, client_id: str) -> dict[str, Any]:
    chat = await chats.chat(chat_id)
    chats.writable(chat)
    if thread_ts:
        return await _to_thread(chats, chat, text=text, ts=thread_ts, client_id=client_id)
    return await _to_timeline(chats, chat, text=text, client_id=client_id)


async def _to_thread(chats: Chats, chat: Mapping[str, Any], *, text: str, ts: str, client_id: str) -> dict[str, Any]:
    engine = chats.engine

    def known(conn) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        row = conn.execute(f"SELECT {rows.MESSAGE_COLUMNS} FROM messages WHERE chat_id = ? AND client_id = ?",
                           (chat["id"], client_id)).fetchone()
        message = rows.message_view(row) if row else None
        wake = message and conn.execute("SELECT state FROM wakes WHERE id = ?", (_wake_id(message),)).fetchone()
        return rows.get_thread(conn, chat["id"], ts), message, wake[0] if wake else None
    thread, retried, wake_state = await engine.store.read(known)
    if retried is not None:
        # Stored and delivered the first time (its reopen was in that
        # transaction). Its wake can't stand in for this check: the retention
        # prune removes a consumed wake after 30 days, and the same wake id
        # would then run the message again. What the first POST may not have
        # finished is the resume: while its message still waits to run, the
        # retry resumes the session. Only this thread's: a client_id is unique
        # in the chat, so the stored message may be another thread's.
        if thread is not None and retried["thread_ts"] == ts and wake_state in ("queued", "requeued"):
            await _resume_if_paused(engine, thread["session_id"])
        return retried
    if thread is None:
        raise ChatRefused("thread_not_found", f"There is no thread {ts!r} in this chat.", status=404)
    stored: dict[str, Any] = {}

    def store(w: Writes) -> None:
        stored.update(rows.add_message(w, chat_id=chat["id"], thread_ts=ts, author_kind="owner", author_id=OWNER,
                                       text=text, client_id=client_id))
        if rows.get_thread(w, chat["id"], ts)["state"] in ("resolved", "paused"):
            rows.update_thread(w, chat["id"], ts, state="working", resolved_at=None)
    await engine.deliver(thread["session_id"], lambda w: _owner_wake(chat, stored, typed=text)(w), also=store)
    await _resume_if_paused(engine, thread["session_id"])
    return stored


async def _resume_if_paused(engine: Engine, session_id: str) -> None:
    """A person's message in a paused thread resumes its session (§0.50)."""
    if (await engine.get_session(session_id))["paused"]:
        await engine.resume(session_id)


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
            chats.spawn(_route_with_model(chats, chat, dict(stored), routing, pending[0], typed=text),
                        f"route:{stored['id']}")
        return stored
    await to_coordinator(chats, chat, stored, also=store, typed=text)
    return stored


async def to_coordinator(chats: Chats, chat: Mapping[str, Any], message: Mapping[str, Any], *,
                         also=None, note: str | None = None, typed: str | None = None) -> None:
    """Wake the chat's coordinator with the person's message (stored by
    ``also`` in the same transaction, or already), as they ``typed`` it when
    known. A coordinator retired meanwhile hands it to its successor."""
    wake = _owner_wake(chat, message, typed=typed, note=note)
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


async def as_coordinator(chats: Chats, chat: Mapping[str, Any], decision: str, tool: str, *,
                         unchosen: str | None = None, **arguments: Any) -> dict[str, Any]:
    """A comms call made on the coordinator's behalf by the router decision
    ``decision`` (a decision model's routing, ch 11 §3.1): recorded as the
    coordinator's, through the same tool and its checks; its hops point to
    the decision (§16.2). ``unchosen``: why a thread it starts has no model
    and effort of the decision's choosing."""
    coordinator = await chats.with_coordinator(chat, lambda w, session_id: session_id)
    caller = await calls.caller_of(chats.engine, coordinator, decision=decision, unchosen=unchosen)
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
                            routing: Mapping[str, Any], decision: str, *, typed: str) -> None:
    """Classify the message with the chosen decision model and route it;
    anything it doesn't route goes to the coordinator, with why (D12). The
    pending ``decision`` is settled with the route, in the transaction that
    wakes the coordinator when it routed nothing.

    The decision model, a third party, reads the stored (redacted) text.
    What it routes goes on as citations of the stored row; only the
    coordinator it hands the message to gets it as the person ``typed`` it."""
    engine = chats.engine
    provider = routing["decision_model"]
    try:
        chosen = await engine.store.read(lambda conn: router.read_setting(conn, router.MODELS_KEY, {}))
        threads = await engine.store.read(lambda conn: rows.list_threads(conn, chat["id"]))
        names = await asyncio.to_thread(project_folders) if chat["kind"] == rows.WORKSPACE else []
        projects = await asyncio.to_thread(lambda: {n: describe_project(n) or n for n in names})
        catalog = await _thread_catalog(chats) if chat["kind"] == rows.PROJECT else None
        route = await router.classify(provider=provider, model=(chosen.get(provider) or {}).get("model") or provider,
                                      kind=chat["kind"], text=message["text"], threads=threads, projects=projects,
                                      threshold=routing["abstain_below"], catalog=catalog)
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
                    note=f"The {provider} decision model handed this message to you ({why}).", typed=typed)


async def _thread_catalog(chats: Chats) -> Catalog | None:
    """What a new thread's model and effort are chosen from; None when it
    can't be read within ``CATALOG_WAIT_S`` (the model isn't asked, and a
    thread it starts runs on the default, saying why). Bounded so a cold
    models probe never holds the person's message back for a minute."""
    try:
        return await chats.thread_catalog(bounded=True)
    except ChatRefused as exc:
        log.warning("storm chats: no catalog for a decision model's thread setup (%s): %s", exc.code, exc.message)
        return None


async def hand_over(chats: Chats, chat: Mapping[str, Any], message: Mapping[str, Any], decision: str,
                    route: dict[str, Any], *, note: str, typed: str | None = None) -> None:
    """Settle the decision and wake the coordinator with the message, in one
    transaction. When the coordinator can't take it (it can't start), the
    decision says so and the timeline tells the person (the POST answered
    long ago). After a restart (``recover``) only the stored row is left, so
    the coordinator reads it redacted."""
    try:
        await to_coordinator(chats, chat, message, also=lambda w: router.settle(w, decision, route), note=note,
                             typed=typed)
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
        started = await as_coordinator(chats, chat, decision, "start_thread_session", message_id=message["id"],
                                       title=router.title_from(message["text"]),
                                       instructions=ROUTED_BRIEF.format(provider=provider),
                                       project_ack="On it, starting a thread.", unchosen=action.pop("unchosen", None),
                                       **action.get("setup", {}))
        if started.get("note"):
            action["note"] = started["note"]        # the decision row says why it runs on the default
        return True
    thread = await chats.engine.store.read(lambda conn: rows.get_thread(conn, chat["id"], action["thread_id"]))
    await as_coordinator(chats, chat, decision, "message_thread", thread_id=action["thread_id"],
                         context_message_ids=[message["id"]])
    await as_coordinator(chats, chat, decision, "post_message",
                         text=f"Sent to [{thread['title'] if thread else 'its thread'}](#{action['thread_id']}).")
    return True
