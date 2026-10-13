"""The workspace coordinator's tools: route a message to the right project(s).

The workspace chat is one chat across every project. Its coordinator
sees all of them (it runs in the projects root and may read any of them
through read-only readers), and these tools are how it routes:

- ``list_projects``: every project, its description and open threads;
- ``fetch_project_card``: one project's threads and their cards;
- ``forward_to_project``: the server copies the owner's message into that
  project's timeline (a new message id, ``forwarded_from`` pointing back) and
  wakes the project's coordinator with it; several calls fan one message out
  to several projects. With only a note it answers a project coordinator's
  ``ask_workspace`` question;
- ``no_such_project``: says, in the workspace chat, that there is no such
  project (nothing is ever created).

Registered into ``comms`` when this module is imported.
"""

from __future__ import annotations

import json
from typing import Optional

from services.cowork_agent import project_layout
from services.projects_chat import comms, coordinators, envelope, store, wakes

_S = {"type": "string"}


def _projects() -> list[str]:
    root = project_layout.xo_projects_root()
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def _describe(name: str) -> str:
    meta = project_layout.load_project(name) or {}
    if meta.get("description"):
        return str(meta["description"])[:300]
    for readme in ("README.md", "README", "PROJECT.md"):
        path = project_layout.project_dir(name) / readme
        try:
            lines = [l.strip("# ").strip() for l in path.read_text(encoding="utf-8", errors="replace").splitlines()]
        except OSError:
            continue
        text = " ".join(l for l in lines[:12] if l)
        if text:
            return text[:300]
    return ""


def _require_project(name: object) -> str:
    if not isinstance(name, str) or name not in _projects():
        raise comms.CommsError("no_such_project", f"There is no project {name!r}. Use list_projects, or "
                                                  "no_such_project to tell the owner.")
    return name


def list_projects(c: comms.Caller, a: dict) -> dict:
    out = []
    for name in _projects():
        threads = store.list_threads(name)
        open_threads = [t for t in threads if t.get("state") != "resolved"]
        out.append({"project": name, "description": _describe(name),
                    "open_threads": [{"thread_id": t["ts"], "title": t.get("title"), "state": t.get("state")}
                                     for t in open_threads[:10]]})
    return {"projects": out}


def fetch_project_card(c: comms.Caller, a: dict) -> dict:
    name = _require_project(a.get("project"))
    threads = store.list_threads(name)
    return {"project": name, "description": _describe(name),
            "threads": [{"thread_id": t["ts"], "title": t.get("title"), "state": t.get("state"),
                         "headline": (t.get("card") or {}).get("headline"),
                         "happened": (t.get("card") or {}).get("happened"),
                         "last_activity_at": t.get("last_activity_at")} for t in threads[:20]]}


def forward_to_project(c: comms.Caller, a: dict) -> dict:
    name = _require_project(a.get("project"))
    ids = list(a.get("context_message_ids") or [])[:8]
    note = a.get("note") if isinstance(a.get("note"), str) and a.get("note").strip() else None
    if not ids and not note:
        raise comms.CommsError("empty_forward", "Pass the owner's message id in context_message_ids, a note, or both.")
    store.ensure_chat(name)
    ch = store.chat(name)
    copies = []
    for message_id in ids:
        row = store.get_message(None, message_id)
        if row is None:
            raise comms.CommsError("no_such_message", f"There is no message {message_id!r} in the workspace chat.")
        if row.get("author_kind") != "owner":
            raise comms.CommsError("not_an_owner_message", "Only the owner's own messages can be forwarded.")
        copies.append(store.post_message(name, author_kind="owner", author_id=row.get("author_id") or "owner",
                                         text=row["text"],
                                         forwarded_from={"chat_id": store.WORKSPACE_CHAT_ID,
                                                         "message_id": message_id}))
    parts = []
    if note or copies:
        parts.append(envelope.relay(sender="workspace-coordinator", sender_session=c.session_id,
                                    cited=[], note=note, reason="forward" if copies else "answer"))
    for copy in copies:
        parts.append(envelope.wake(reason="mention", chat_id=ch.id, chat_kind=ch.kind, trigger=copy))
    target = coordinators.ensure(name)
    wakes.enqueue(name, target["id"], reason="forward" if copies else "relay", envelope="\n\n".join(parts),
                  meta={"message_id": copies[0]["id"]} if copies else {})
    return {"project": name, "forwarded_message_ids": [m["id"] for m in copies], "coordinator": target["id"]}


def no_such_project(c: comms.Caller, a: dict) -> dict:
    text = a.get("text") if isinstance(a.get("text"), str) and a.get("text").strip() else None
    if text is None:
        raise comms.CommsError("missing_argument", "text is required: one short line for the owner.")
    row = store.post_message(None, author_kind="agent", author_id="coordinator", author_session_id=c.session_id,
                             text=text[:1000])
    return {"message_id": row["id"]}


comms.register("list_projects", list_projects, {
    "description": "Every project in the workspace: its folder name, description and open threads.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}, roles=("workspace-coordinator",))
comms.register("fetch_project_card", fetch_project_card, {
    "description": "One project's threads with their state and latest card.",
    "inputSchema": {"type": "object", "properties": {"project": _S}, "required": ["project"],
                    "additionalProperties": False},
}, roles=("workspace-coordinator",))
comms.register("forward_to_project", forward_to_project, {
    "description": "Send the owner's message to a project's coordinator: the server copies the messages in "
                   "context_message_ids into that project's chat, verbatim. note is yours (only what the project "
                   "needs beyond the owner's words). Call once per project when a message touches several. With "
                   "only a note, it answers a project coordinator's question.",
    "inputSchema": {"type": "object", "properties": {
        "project": _S, "note": _S, "context_message_ids": {"type": "array", "items": _S, "maxItems": 8}},
        "required": ["project"], "additionalProperties": False},
}, roles=("workspace-coordinator",))
comms.register("no_such_project", no_such_project, {
    "description": "Tell the owner, in one short line, that the project they named does not exist. Never create one.",
    "inputSchema": {"type": "object", "properties": {"text": _S}, "required": ["text"], "additionalProperties": False},
}, roles=("workspace-coordinator",), terminal_for=("workspace-coordinator",))
