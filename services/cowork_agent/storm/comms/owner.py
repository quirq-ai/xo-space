"""The tools that reach the person (design ch 12 §5.2–§5.5): asks, the
coordinators' view of the review queue, and proposing a project.

An ask returns at once with its item's id; the answer comes back later as a
``review-answered`` wake built by the server (ch 11 §4). Its risk is computed
by XO's policy from the exact ``detail`` (a command, paths, a URL), never
taken from the agent: the agent's words can't lower it (ch 12 §5.2).
Coordinators may annotate an item, never decide it (§0.55); no tool decides
anything, and only the review route writes an owner decision (I4).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from services.cowork_agent import project_layout
from services.cowork_agent.storm import chats as chats_pkg
from services.cowork_agent.storm.chats import router, rows
from services.cowork_agent.storm.comms import args as a
from services.cowork_agent.storm.comms.calls import CommsError, ToolCall, current_turn
from services.cowork_agent.storm.comms.talk import _cited
from services.cowork_agent.storm.comms.tools import (
    ASK_TITLE_MAX, ID_MAX, MAX_CITED, MAX_OPTIONS, MAX_PATHS, NOTE_MAX, TEXT_MAX, WHY_MAX, WORD_MAX,
)
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.engine.places import policy_view
from services.cowork_agent.storm.review import Action, PolicyContext
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.review.policy import assess
from services.cowork_agent.storm.review.tiers import Tier

ASK_KINDS = ("question", "approval", "action")
GUIDANCE = {True: "End your turn now; the owner's answer arrives as a new wake.",
            False: "Keep working on your recommended option; the owner's answer arrives as a new wake."}


def _detail(t: ToolCall) -> dict[str, Any]:
    raw = t.args.get("detail") or {}
    if not isinstance(raw, Mapping) or set(raw) - {"command", "diff", "paths", "url", "text"}:
        raise CommsError("invalid_argument", "detail holds only command, diff, paths, url and text.")
    detail: dict[str, Any] = {k: a.text(raw, k, required=False, limit=TEXT_MAX)
                              for k in ("command", "diff", "url", "text") if raw.get(k) is not None}
    if raw.get("paths") is not None:
        detail["paths"] = a.ids(raw, "paths", limit=MAX_PATHS)
    return detail


async def _risk(t: ToolCall, detail: Mapping[str, Any]) -> tuple[Tier | None, tuple[str, ...]]:
    """The tier XO's policy gives what the ask names; None when it names
    nothing XO can judge."""
    caller = t.caller
    if not any(detail.get(k) for k in ("command", "paths", "url")):
        return None, ()
    preset, project_pid = await t.engine.store.read(lambda conn: conn.execute(
        "SELECT s.preset, c.project_pid FROM sessions s LEFT JOIN chats c ON c.id = s.chat_id WHERE s.id = ?",
        (caller.session_id,)).fetchone())
    places, ref = await asyncio.to_thread(
        policy_view, session_id=caller.session_id, role=caller.role, preset=preset, cwd=caller.cwd,
        thread_ts=caller.thread_ts, project_pid=project_pid, project_config_paths=t.engine.project_config_paths)
    ctx = PolicyContext(places=places, now=iso(t.engine.clock.time()), xo_ports=t.engine.xo_ports)
    actions = []
    if detail.get("command"):
        actions.append(Action(kind="command", tool="ask_owner", session=ref, command=detail["command"]))
    if detail.get("paths"):
        actions.append(Action(kind="edit_file", tool="ask_owner", session=ref, paths=tuple(detail["paths"])))
    if detail.get("url"):
        actions.append(Action(kind="web_fetch", tool="ask_owner", session=ref, url=detail["url"]))
    found = await asyncio.to_thread(lambda: [assess(action, ctx) for action in actions])
    return max(f.tier for f in found), tuple(dict.fromkeys(r for f in found for r in f.reasons))


async def ask_owner(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    kind = a.choice(t.args, "kind", ASK_KINDS, required=True)
    title = a.text(t.args, "title", limit=ASK_TITLE_MAX)
    why = a.text(t.args, "why", limit=WHY_MAX)
    detail = _detail(t)
    blocking = a.flag(t.args, "blocking", default=False)
    options = t.args.get("options") or []
    if options and kind != "question":
        raise CommsError("invalid_argument", "options are for a question; an approval or an action has its own.")
    if not isinstance(options, list) or len(options) > MAX_OPTIONS or not all(
            isinstance(o, Mapping) and a.text(o, "id", limit=ID_MAX) and a.text(o, "label", limit=WORD_MAX) for o in options):
        raise CommsError("invalid_argument", f"options is a list of at most {MAX_OPTIONS} {{id, label, consequence}}.")
    recommended = a.text(t.args, "recommended", required=False, limit=ID_MAX)
    if recommended and recommended not in {o["id"] for o in options}:
        raise CommsError("invalid_argument", "recommended must be the id of one of the options.")
    if kind == "question":
        # The decision route answers a question by an option's label or in words.
        detail["questions"] = [{"id": "answer", "question": title, "options": [
            {"label": o["label"], "description": o.get("consequence") or "", "id": o["id"]} for o in options]}]
    if recommended:
        detail["recommended"] = recommended
    risk, reasons = await _risk(t, detail)

    def fn(w: Writes) -> dict[str, Any]:
        item = t.chats.queue.open_item(w, ItemSpec(
            kind=kind, title=title, why=why, origin="agent", session_id=caller.session_id, chat_id=caller.chat_id,
            thread_ts=caller.thread_ts, turn_id=current_turn(w, caller.session_id), epoch=caller.epoch, risk=risk,
            risk_reasons=reasons, blocking=blocking, detail=detail))
        return t.record(w, {"review_id": item, "state": "open", "guidance": GUIDANCE[blocking]}, blocking=blocking)
    return await t.engine.write(fn)


def _own_item(w: Writes, t: ToolCall, review_id: str, *, in_chat: bool) -> tuple[str, str]:
    """``(state, session)`` of an item the caller may touch: an ask it raised
    itself (never a CLI's permission or XO's own item, ch 12 §5.3), or, for a
    coordinator, any item of its chat."""
    row = w.execute("SELECT state, session_id, chat_id, origin, kind FROM review_items WHERE id = ?",
                    (review_id,)).fetchone()
    mine = row is not None and (row[2] == t.caller.chat_id if in_chat else
                                (row[1], row[3]) == (t.caller.session_id, "agent") and row[4] in ASK_KINDS)
    if not mine:
        raise CommsError("no_such_review", f"There is no item {review_id!r} you can reach.")
    return row[0], row[1]


async def withdraw_ask(t: ToolCall) -> dict[str, Any]:
    review_id = a.text(t.args, "review_id")
    reason = a.text(t.args, "reason", limit=NOTE_MAX)

    def fn(w: Writes) -> dict[str, Any]:
        state, _session = _own_item(w, t, review_id, in_chat=False)
        if state != "open":
            raise CommsError("not_open", f"{review_id} is {state}; only an open ask can be withdrawn.")
        _annotate(w, review_id, by="agent", session=t.caller.session_id, note=reason)
        t.chats.queue.withdraw(w, review_id, "withdrawn_by_agent")
        return {"review_id": review_id, "state": "withdrawn"}
    return await t.write(fn)


async def list_review(t: ToolCall) -> dict[str, Any]:
    a.choice(t.args, "state", ("open",))

    def fn(w: Writes) -> dict[str, Any]:
        found = w.execute("SELECT id, thread_ts, kind, risk, title, created_at FROM review_items WHERE chat_id = ? "
                          "AND state = 'open' ORDER BY urgency, seq", (t.caller.chat_id,)).fetchall()
        return {"items": [dict(zip(("review_id", "thread_id", "kind", "risk", "title", "created_at"), r))
                          for r in found]}
    return await t.write(fn)


async def annotate_review(t: ToolCall) -> dict[str, Any]:
    """One line under an item, labelled as the coordinator's; it changes
    nothing else about the item and decides nothing (§0.55)."""
    review_id = a.text(t.args, "review_id")
    note = a.text(t.args, "note", limit=NOTE_MAX)

    def fn(w: Writes) -> dict[str, Any]:
        state, _session = _own_item(w, t, review_id, in_chat=True)
        if state != "open":
            raise CommsError("not_open", f"{review_id} is {state}; only an open item can be annotated.")
        _annotate(w, review_id, by="coordinator", session=t.caller.session_id, note=note)
        return {"ok": True, "review_id": review_id}
    return await t.write(fn)


def _annotate(w: Writes, review_id: str, *, by: str, session: str, note: str) -> None:
    row = w.execute("SELECT annotations, chat_id, thread_ts, session_id FROM review_items WHERE id = ?",
                    (review_id,)).fetchone()
    entry = {"by": by, "session_id": session, "note": note, "at": w.now}
    w.execute("UPDATE review_items SET annotations = json(?) WHERE id = ?",
              (json.dumps([*json.loads(row[0]), entry]), review_id))
    w.append("review.annotated", {"id": review_id, **entry}, chat_id=row[1], thread_ts=row[2], session_id=row[3])


async def propose_project(t: ToolCall) -> dict[str, Any]:
    """The workspace coordinator proposes a project; only the person creates
    one (ch 11 §9): a ``create_project`` item, notable."""
    caller = t.caller
    name = a.text(t.args, "name", limit=ID_MAX)
    purpose = a.text(t.args, "purpose", limit=WHY_MAX)
    why_new = a.text(t.args, "why_new", limit=WHY_MAX)
    message_ids = a.ids(t.args, "context_message_ids", limit=MAX_CITED)
    first_task = a.text(t.args, "first_task", required=False, limit=WHY_MAX)
    if not message_ids:
        raise CommsError("missing_argument", "context_message_ids must name the owner's message that asks for it.")
    root = Path(project_layout.xo_projects_root())
    if not chats_pkg.valid_project_name(name) or Path(name).name != name:
        raise CommsError("invalid_name", "A project's name is one folder name: letters, digits, '-' or '_', at most "
                                         "64, not starting with a dot.")
    taken = {n.lower() for n in await asyncio.to_thread(lambda: [p.name for p in root.iterdir()])}
    if name.lower() in taken:
        raise CommsError("project_exists", f"There is already a folder called {name!r} (or one differing only in "
                                           "case). Forward to it, or propose another name.")
    names = await asyncio.to_thread(chats_pkg.project_folders)
    described = await asyncio.to_thread(lambda: {n: chats_pkg.describe_project(n) or n for n in names})
    suggestion = await router.owning_project(t.engine, caller.chat_id, proposal=f"{name}: {purpose}",
                                             projects=described)

    def fn(w: Writes) -> dict[str, Any]:
        cited = _cited(w, t, message_ids)
        item = t.chats.queue.open_item(w, ItemSpec(
            kind="create_project", title=f"Create the project “{name}”", why=why_new, origin="agent",
            session_id=caller.session_id, chat_id=caller.chat_id, turn_id=current_turn(w, caller.session_id),
            epoch=caller.epoch, risk=Tier.NOTABLE, risk_reasons=("creates a project folder",),
            detail={"name": name, "purpose": purpose, "why_new": why_new, "first_task": first_task,
                    "context_message_ids": [m["id"] for m in cited],
                    **({"suggestion": suggestion} if suggestion else {})}))
        # ch 12 §5.5: the workspace chat says so in one line (a terminal call ends the agent's turn).
        rows.add_message(w, chat_id=caller.chat_id, author_kind="system", author_id="xo",
                         text=f"No project fits this, so a new one, “{name}”, was proposed; only you can create it.")
        return t.record(w, {"review_id": item, "state": "open",
                            "guidance": "Only the owner can create it; the chat tells them. You are done."})
    return await t.engine.write(fn)
