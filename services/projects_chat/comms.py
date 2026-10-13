"""The comms tools: the only way a coordinator or a thread is heard.

Every agent session gets an MCP server (``xo_comms``, served by
``routers/cowork_agent/space_comms.py``) whose tools depend on its role. Text
an agent writes outside these tools reaches no one. A successful call to a
*terminal* tool ends a turn properly; a turn that ends without one is
re-prompted once by the turn engine (the reply gate).

Each call is checked against the caller's role and scope (a thread acts on
its own thread only; a coordinator on its own channel only), applied to the
store, and logged as a ``comms.call`` line in the session's ``events.jsonl``,
which is what a coordinator's routing decision is built from. Calls that hand
work on (start a thread, forward, ask) queue a wake for the receiving session;
the turn engine runs it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from services.errors import ServiceError
from services.projects_chat import coordinators, envelope, store, wakes

log = logging.getLogger(__name__)

SERVER_NAME = "xo_comms"


class CommsError(ServiceError):
    """A tool call was refused; the message goes back to the agent."""


@dataclass
class Caller:
    project: Optional[str]
    session: dict

    @property
    def role(self) -> str:
        return self.session["role"]

    @property
    def session_id(self) -> str:
        return self.session["id"]


# ── tool definitions ──────────────────────────────────────────────────────────

def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_S = {"type": "string"}
_IDS = {"type": "array", "items": {"type": "string"}, "maxItems": 8}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}

TOOLS: dict[str, dict] = {
    "post_message": {
        "description": "Post a message in this channel's chat. This is the only way the user reads your words "
                       "here; it notifies everyone. Keep it short.",
        "inputSchema": _schema({"text": _S}, ["text"]),
    },
    "start_thread_session": {
        "description": "Start a thread: a separate agent session with its own context, in its own git worktree "
                       "of this project, anchored on the user's message (message_id). The server copies the "
                       "anchor and context_message_ids verbatim into the thread. instructions is your brief: the "
                       "user's words are already attached, so add only what the thread needs from memory, the "
                       "default you chose where the ask forks, and which thread owns a shared file. ack is the "
                       "thread's first line; project_ack is one short, varied line in this chat.",
        "inputSchema": _schema({"message_id": _S, "title": _S, "instructions": _S, "context_message_ids": _IDS,
                                "ack": _S, "project_ack": _S}, ["message_id", "title", "instructions"]),
    },
    "message_thread": {
        "description": "Forward a follow-up to an existing thread (thread_id = the thread's root message id). Pass "
                       "the user's message id in context_message_ids; the note is yours. Reopens a resolved thread. "
                       "Then post one line saying where it went.",
        "inputSchema": _schema({"thread_id": _S, "note": _S, "context_message_ids": _IDS}, ["thread_id"]),
    },
    "list_thread_sessions": {
        "description": "This project's threads: id, title, state, latest card headline, last activity.",
        "inputSchema": _schema({}, []),
    },
    "fetch_thread": {
        "description": "A thread's status, latest card and recent messages. A thread may omit thread_id for its own.",
        "inputSchema": _schema({"thread_id": _S, "limit": _LIMIT}, []),
    },
    "fetch_timeline": {
        "description": "Recent messages in this channel's chat, oldest first.",
        "inputSchema": _schema({"limit": _LIMIT}, []),
    },
    "set_thread_resolved": {
        "description": "Mark a thread resolved (nothing more is owed) or reopen it.",
        "inputSchema": _schema({"thread_id": _S, "resolved": {"type": "boolean"}}, ["resolved"]),
    },
    "ask_workspace": {
        "description": "Ask the workspace coordinator about anything outside this project. The answer arrives "
                       "later as a relay; end your turn after asking.",
        "inputSchema": _schema({"question": _S}, ["question"]),
    },
    "reply": {
        "description": "Post in your thread. The only way the user and the coordinator see your result.",
        "inputSchema": _schema({"text": _S}, ["text"]),
    },
    "update_status": {
        "description": "Replace your thread's live checklist (✱ in progress, ○ to do, ✓ done). Notifies no one.",
        "inputSchema": _schema({"text": _S, "start_new": {"type": "boolean"}}, ["text"]),
    },
    "react": {
        "description": "React to a message with an emoji (e.g. to acknowledge thanks).",
        "inputSchema": _schema({"message_id": _S, "emoji": _S}, ["message_id", "emoji"]),
    },
    "no_reply_needed": {
        "description": "End the turn without saying anything (duplicate wake, people talking to each other, nothing to add).",
        "inputSchema": _schema({"reason": _S}, ["reason"]),
    },
}

ROLE_TOOLS = {
    "project-coordinator": ("post_message", "start_thread_session", "message_thread", "list_thread_sessions",
                            "fetch_thread", "fetch_timeline", "set_thread_resolved", "ask_workspace", "react",
                            "no_reply_needed"),
    "thread": ("reply", "update_status", "set_thread_resolved", "fetch_thread", "fetch_timeline", "react",
               "no_reply_needed"),
    "workspace-coordinator": ("post_message", "fetch_timeline", "react", "no_reply_needed"),
}

TERMINAL = {
    "project-coordinator": ("post_message", "react", "no_reply_needed"),
    "thread": ("reply", "update_status", "no_reply_needed"),
    "workspace-coordinator": ("post_message", "react", "no_reply_needed"),
}


def tools_for(role: str) -> list[dict]:
    return [{"name": name, **TOOLS[name]} for name in ROLE_TOOLS.get(role, ())]


def terminal_tool_names(role: str) -> list[str]:
    """Fully qualified names, as the agent sees them (``mcp__xo_comms__reply``)."""
    return [f"mcp__{SERVER_NAME}__{name}" for name in TERMINAL.get(role, ())]


# ── helpers ───────────────────────────────────────────────────────────────────

def _text(args: dict, key: str, *, required: bool = True, limit: int = 20000) -> Optional[str]:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise CommsError("missing_argument", f"{key} is required.")
        return None
    if not isinstance(value, str):
        raise CommsError("invalid_argument", f"{key} must be text.")
    return value[:limit]


def _cited_rows(project: Optional[str], ids: list[str], ch_id: str) -> list[tuple[dict, str]]:
    rows = []
    for message_id in ids[:8]:
        row = store.get_message(project, message_id)
        if row is None:
            raise CommsError("no_such_message", f"There is no message {message_id!r} in this channel.")
        rows.append((row, ch_id))
    return rows


def _own_thread(caller: Caller, args: dict) -> str:
    """The thread a call is about: a thread's own, or one the coordinator named."""
    if caller.role == "thread":
        given = args.get("thread_id")
        if given and given != caller.session.get("thread_ts"):
            raise CommsError("not_your_thread", "A thread can only act on its own thread.")
        return caller.session["thread_ts"]
    thread_id = _text(args, "thread_id")
    if store.get_thread(caller.project, thread_id) is None:
        raise CommsError("no_such_thread", f"There is no thread {thread_id!r} in this project.")
    return thread_id


def _thread_session(project: Optional[str], thread: dict) -> dict:
    session = store.get_session(project, thread.get("session_id") or "")
    if session is None:
        raise CommsError("thread_has_no_session", "That thread has no agent session.")
    return session


# ── the tools ─────────────────────────────────────────────────────────────────

def _post_message(c: Caller, a: dict) -> dict:
    row = store.post_message(c.project, author_kind="agent", author_id="coordinator",
                             author_session_id=c.session_id, text=_text(a, "text"))
    return {"message_id": row["id"]}


def _start_thread_session(c: Caller, a: dict) -> dict:
    anchor_id = _text(a, "message_id")
    anchor = next((r for r in store.read_messages(c.project) if r.get("id") == anchor_id), None)
    if anchor is None:
        raise CommsError("no_such_message", f"{anchor_id!r} is not a message in this chat's timeline.")
    existing = store.get_thread(c.project, anchor_id)
    if existing is not None:
        return {"already_running": True, "thread_id": anchor_id,
                "hint": "Use message_thread to send this thread a follow-up."}
    ch = store.channel(c.project)
    thread_session = store.create_session(c.project, role="thread", cwd=coordinators.cwd_for(c.project),
                                          thread_ts=anchor_id)
    thread_session.pop("token", None)
    store.create_thread(c.project, root_id=anchor_id, title=_text(a, "title", limit=120),
                        session_id=thread_session["id"], created_by_session=c.session_id)
    if project_ack := _text(a, "project_ack", required=False, limit=300):
        store.post_message(c.project, author_kind="agent", author_id="coordinator",
                           author_session_id=c.session_id, text=project_ack)
    if ack := _text(a, "ack", required=False, limit=500):
        store.post_message(c.project, author_kind="agent", author_id="coordinator",
                           author_session_id=c.session_id, text=ack, thread_ts=anchor_id)
    extra = [i for i in (a.get("context_message_ids") or []) if i != anchor_id]
    cited = _cited_rows(c.project, extra, ch.id)
    brief = envelope.relay(sender="coordinator", sender_session=c.session_id, cited=cited,
                           note=_text(a, "instructions"), reason="spawn")
    wakes.enqueue(c.project, thread_session["id"], reason="spawn", envelope=brief, priority="next")
    first = envelope.wake(reason="mention", channel_id=ch.id, channel_kind=ch.kind, trigger=anchor,
                          role="initiator")
    wakes.enqueue(c.project, thread_session["id"], reason="mention", envelope=first, priority="next")
    return {"thread_id": anchor_id, "session_id": thread_session["id"]}


def _message_thread(c: Caller, a: dict) -> dict:
    thread_id = _own_thread(c, a)
    thread = store.get_thread(c.project, thread_id)
    reopened = thread.get("state") == "resolved"
    if reopened:
        store.update_thread(c.project, thread_id, state="working", resolved_at=None)
    ch = store.channel(c.project)
    cited = _cited_rows(c.project, list(a.get("context_message_ids") or []), ch.id)
    note = _text(a, "note", required=False)
    if not cited and not note:
        raise CommsError("empty_relay", "Pass the user's message in context_message_ids, a note, or both.")
    text = envelope.relay(sender="coordinator", sender_session=c.session_id, cited=cited, note=note)
    wakes.enqueue(c.project, _thread_session(c.project, thread)["id"], reason="relay", envelope=text)
    return {"delivered": True, "thread_id": thread_id, "reopened": reopened}


def _list_thread_sessions(c: Caller, a: dict) -> dict:
    out = []
    for t in store.list_threads(c.project):
        card = t.get("card") or {}
        out.append({"thread_id": t["ts"], "title": t.get("title"), "state": t.get("state"),
                    "headline": card.get("headline"), "needs_you": card.get("needs_you"),
                    "last_activity_at": t.get("last_activity_at")})
    return {"threads": out}


def _brief(row: dict) -> dict:
    return {k: row.get(k) for k in ("id", "author_kind", "author_id", "written_at", "text") if row.get(k) is not None}


def _fetch_thread(c: Caller, a: dict) -> dict:
    thread_id = c.session["thread_ts"] if (c.role == "thread" and not a.get("thread_id")) else _own_thread(c, a)
    thread = store.get_thread(c.project, thread_id) or {}
    limit = int(a.get("limit") or 20)
    root = store.get_message(c.project, thread_id)
    return {"thread_id": thread_id, "title": thread.get("title"), "state": thread.get("state"),
            "status": store.read_status(c.project, thread_id),
            "card": store.get_card(c.project, thread_id),
            "root": _brief(root) if root else None,
            "messages": [_brief(r) for r in store.read_messages(c.project, thread_ts=thread_id, limit=limit)]}


def _fetch_timeline(c: Caller, a: dict) -> dict:
    limit = int(a.get("limit") or 30)
    return {"messages": [_brief(r) for r in store.read_messages(c.project, limit=limit)]}


def _set_thread_resolved(c: Caller, a: dict) -> dict:
    thread_id = _own_thread(c, a)
    resolved = bool(a.get("resolved"))
    before = store.get_thread(c.project, thread_id) or {}
    doc = store.update_thread(c.project, thread_id, state="resolved" if resolved else "working",
                              resolved_at=store.now_ms_iso() if resolved else None)
    return {"thread_id": thread_id, "resolved": resolved, "resolved_at": doc.get("resolved_at"),
            "transitioned": (before.get("state") == "resolved") != resolved}


def _ask_workspace(c: Caller, a: dict) -> dict:
    question = _text(a, "question", limit=4000)
    workspace = coordinators.ensure(None)
    row = store.post_message(None, author_kind="agent", author_id=f"coordinator:{c.project}",
                             author_session_id=c.session_id, text=question)
    text = envelope.relay(sender="coordinator", sender_session=c.session_id, cited=[],
                          note=f"Project {c.project!r} asks (answer in its chat with forward_to_project): {question}",
                          reason="ask")
    wakes.enqueue(None, workspace["id"], reason="ask", envelope=text, meta={"from_project": c.project,
                                                                          "message_id": row["id"]})
    return {"asked": True, "note": "The answer will arrive as a relay. End your turn now."}


def _reply(c: Caller, a: dict) -> dict:
    thread_id = c.session["thread_ts"]
    row = store.post_message(c.project, author_kind="agent", author_id="thread", author_session_id=c.session_id,
                             text=_text(a, "text"), thread_ts=thread_id)
    store.update_thread(c.project, thread_id)
    return {"message_id": row["id"], "thread_id": thread_id}


def _update_status(c: Caller, a: dict) -> dict:
    store.write_status(c.project, c.session["thread_ts"], _text(a, "text", limit=4000))
    store.update_thread(c.project, c.session["thread_ts"])
    return {"ok": True}


def _react(c: Caller, a: dict) -> dict:
    message_id = _text(a, "message_id")
    row = store.get_message(c.project, message_id)
    if row is None:
        raise CommsError("no_such_message", f"There is no message {message_id!r} here.")
    store.add_reaction(c.project, message_id, emoji=_text(a, "emoji", limit=32), by=c.session_id,
                       thread_ts=row.get("thread_ts"))
    return {"ok": True}


def _no_reply_needed(c: Caller, a: dict) -> dict:
    return {"ok": True, "reason": _text(a, "reason", required=False, limit=200)}


_HANDLERS: dict[str, Callable[[Caller, dict], dict]] = {
    "post_message": _post_message, "start_thread_session": _start_thread_session,
    "message_thread": _message_thread, "list_thread_sessions": _list_thread_sessions,
    "fetch_thread": _fetch_thread, "fetch_timeline": _fetch_timeline,
    "set_thread_resolved": _set_thread_resolved, "ask_workspace": _ask_workspace, "reply": _reply,
    "update_status": _update_status, "react": _react, "no_reply_needed": _no_reply_needed,
}


def register(name: str, handler: Callable[[Caller, dict], dict], definition: dict,
             roles: tuple[str, ...], terminal_for: tuple[str, ...] = ()) -> None:
    """Add a tool from another module (the workspace tools, M2)."""
    TOOLS[name] = definition
    _HANDLERS[name] = handler
    for role in roles:
        if name not in ROLE_TOOLS[role]:
            ROLE_TOOLS[role] = ROLE_TOOLS[role] + (name,)
    for role in terminal_for:
        if name not in TERMINAL[role]:
            TERMINAL[role] = TERMINAL[role] + (name,)


def call(caller: Caller, name: str, args: Any) -> dict:
    """Run one tool for ``caller``. Raises :class:`CommsError` (the agent sees its message)."""
    if name not in ROLE_TOOLS.get(caller.role, ()):
        raise CommsError("unknown_tool", f"{name!r} is not a tool for a {caller.role}.")
    if not isinstance(args, dict):
        raise CommsError("invalid_arguments", "Arguments must be an object.")
    ok, result, error = False, None, None
    try:
        result = _HANDLERS[name](caller, args)
        ok = True
        return result
    except store.StoreError as exc:
        error = exc.message
        raise CommsError(exc.code, exc.message) from exc
    except CommsError as exc:
        error = exc.message
        raise
    finally:
        store.append_session_event(
            caller.project, caller.session_id, "comms.call", tool=name, args=args, ok=ok,
            terminal=name in TERMINAL.get(caller.role, ()), result=result if ok else None, error=error,
        )
        if ok:
            store.update_session(caller.project, caller.session_id, last_comms_at=store.now_ms_iso())


def result_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)
