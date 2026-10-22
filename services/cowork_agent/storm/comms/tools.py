"""The ``xo_comms`` tools: each one's schema and kind, and which roles have
it (design ch 12 §3–§5, §0.54). Pure data, imported by the presets' callers
(the comms mount a session gets) and by the endpoint (``tools/list``).

The token decides the caller's role and the role decides its tools: a role
never sees, and can never call, another's (ch 12 §2.2, §6). The lines of
talk (ch 11 §16.1) are this table plus the handlers' scope checks: a thread
has ``message_coordinator`` (no address) and nothing that reaches the
workspace or another thread; only a project coordinator has
``ask_workspace``; the workspace coordinator reaches a thread only through a
project (``forward_to_project``).

**Terminal** tools end the agent's work properly; **routing** tools hand it
on. Both satisfy the reply gate (ch 11 §5); so does a blocking
``ask_owner``, which the gate reads from the call. The job tools
(``create_job``, ``list_jobs``, ``cancel_job``) arrive with the jobs PR
(ch 18 §1, PR 18).
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from services.cowork_agent.adapters.contract import McpServer
from services.cowork_agent.storm.engine.model import SESSION_TOKEN_ENV
from services.cowork_agent.storm.engine.catalog import Catalog

SERVER_NAME = "xo_comms"
PATH = "/mcp/space-comms"
#: Lists in arguments are bounded (ch 12 §5).
MAX_CITED = 8
MAX_FETCH_IDS = 50
MAX_SHARED = 20
MAX_OPTIONS = 4
TITLE_MAX = 120
LABEL_MAX = 32
NOTE_MAX = 300
#: Text arguments' lengths, by what they hold.
LONG_TEXT_MAX = 4000            # a checklist, a question, a todo's description
SHORT_TEXT_MAX = 1000           # a todo, a purpose
LINE_MAX = 500                  # an acknowledgement
SHORT_LINE_MAX = 300            # a line in the project chat
WORD_MAX = 200                  # a reason, a model id, an option's label
ID_MAX = 64                     # an id or a project name
EMOJI_MAX = 32
FILE_NAME_MAX = 100
MAX_PATHS = 64
TEXT_MAX = 20_000               # a message, a brief, a note to another agent
ASK_TITLE_MAX = 200
WHY_MAX = 1000                  # an ask's why, a proposal's purpose
CAPTION_MAX = 500
MEMORY_VERSION_MAX = 12         # chats.memory.VERSION_HEX (a test keeps them equal)

_S = {"type": "string"}
_IDS = {"type": "array", "items": _S, "maxItems": MAX_CITED}


def _t(limit: int = TEXT_MAX) -> dict[str, Any]:
    """A text argument and its length, as the handler checks it: stated in
    the schema so the agent never meets a limit it couldn't see."""
    return {"type": "string", "maxLength": limit}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}


def _schema(props: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": dict(props), "required": list(required), "additionalProperties": False}


#: name -> (kind, description, input schema). kind: terminal | routing | read | act.
TOOLS: Mapping[str, tuple[str, str, dict[str, Any]]] = {
    "post_message": ("terminal", "Post a message in this chat. This is the only way the owner reads your words here; "
                     "keep it short.", _schema({"text": _t()}, ("text",))),
    "reply": ("terminal", "Post in your thread. The only way the owner and the coordinator see your result.",
              _schema({"text": _t()}, ("text",))),
    "update_status": ("terminal", "Replace your thread's live checklist (✱ in progress, ○ to do, ✓ done). Notifies "
                      "no one.", _schema({"text": _t(LONG_TEXT_MAX)}, ("text",))),
    "react": ("terminal", "React to a message with an emoji (to acknowledge thanks, for example).",
              _schema({"message_id": _t(), "emoji": _t(EMOJI_MAX)}, ("message_id", "emoji"))),
    "no_reply_needed": ("terminal", "End your turn without saying anything: a duplicate wake, people talking to each "
                        "other, nothing to add.", _schema({"reason": _t(WORD_MAX)}, ("reason",))),
    "forward_to_project": ("routing", "Send the owner's message to a project's coordinator: the server copies the "
                           "messages in context_message_ids into that project's chat, verbatim. note is yours: only "
                           "what the project needs beyond the owner's words. Call once per project when a message "
                           "touches several. With only a note, it answers that project coordinator's question.",
                           _schema({"project": _t(), "note": _t(), "context_message_ids": _IDS}, ("project",))),
    "no_such_project": ("terminal", "Tell the owner, in one short line, that the project their question names doesn't "
                        "exist. For new work that needs a project, use propose_project.",
                        _schema({"text": _t()}, ("text",))),
    "propose_project": ("terminal", "Propose a new project for work no project fits. Call list_projects first. Only "
                        "the owner can approve it; it opens an item for them and returns its id.",
                        _schema({"name": _t(ID_MAX), "purpose": _t(WHY_MAX), "why_new": _t(WHY_MAX),
                                 "context_message_ids": _IDS,
                                 "first_task": _t(WHY_MAX)}, ("name", "purpose", "why_new", "context_message_ids"))),
    "list_projects": ("read", "Every project in the workspace: its folder name, description and open threads.",
                      _schema({})),
    "fetch_project_card": ("read", "One project's threads with their state and latest card.",
                           _schema({"project": _t()}, ("project",))),
    "start_thread_session": ("routing", "Start a thread: a separate agent session with its own context, in its own "
                             "working folder of this project, anchored on the owner's message (message_id). The "
                             "server copies the anchor and context_message_ids verbatim into the thread. instructions "
                             "is your brief: add only what the thread needs beyond the owner's words. ack is the "
                             "thread's first line; project_ack is one short, varied line in this chat. Choose the "
                             "thread's model and effort for this work from the thread agent's catalog: high effort "
                             "for hard work, lower for a small edit. A choice the catalog lacks isn't used: the "
                             "thread then runs on its default, and the result's note says why.",
                             _schema({"message_id": _t(), "title": _t(TITLE_MAX), "instructions": _t(),
                                      "context_message_ids": _IDS, "ack": _t(LINE_MAX),
                                      "project_ack": _t(SHORT_LINE_MAX), "model": _t(WORD_MAX),
                                      "effort": _t(WORD_MAX)},
                                     ("message_id", "title", "instructions"))),
    "message_thread": ("routing", "Send a follow-up to an existing thread (thread_id = the thread's root message id): "
                       "the owner's messages in context_message_ids, your note, or both. It reopens a resolved "
                       "thread, and answers the thread's own question when it asked you one. Then post one line "
                       "saying where it went.",
                       _schema({"thread_id": _t(), "note": _t(), "context_message_ids": _IDS}, ("thread_id",))),
    "ask_workspace": ("routing", "Ask the workspace coordinator about anything outside this project. The answer "
                      "arrives later as a relay; end your turn after asking. on_behalf_of names the thread that "
                      "asked you, if one did.",
                      _schema({"question": _t(LONG_TEXT_MAX), "on_behalf_of": _t()}, ("question",))),
    "message_coordinator": ("routing", "Hand a question or an update to your project coordinator. Its answer arrives "
                            "later; end your turn or keep working. context_message_ids are owner messages the "
                            "server copies for it.", _schema({"text": _t(), "context_message_ids": _IDS}, ("text",))),
    "list_thread_sessions": ("read", "This project's threads: id, title, state, latest card headline, last activity.",
                             _schema({})),
    "fetch_thread": ("read", "A thread's state, checklist, latest card and recent messages. A thread may omit "
                     "thread_id for its own.", _schema({"thread_id": _t(), "limit": _LIMIT})),
    "fetch_timeline": ("read", "Recent messages in this chat, oldest first.", _schema({"limit": _LIMIT})),
    "fetch_messages": ("read", "Exact messages by id, or a page of this chat's timeline or of a thread, with before "
                       "or after a message id.",
                       _schema({"ids": {"type": "array", "items": _S, "maxItems": MAX_FETCH_IDS}, "thread_id": _t(),
                                "before": _t(), "after": _t(), "limit": _LIMIT})),
    "set_thread_resolved": ("act", "Mark a thread resolved (nothing more is owed) or reopen it.",
                            _schema({"thread_id": _t(), "resolved": {"type": "boolean"}}, ("resolved",))),
    "rename_thread": ("act", "Replace a thread's title.",
                      _schema({"thread_id": _t(), "title": _t(TITLE_MAX)}, ("title",))),
    "set_thread_label": ("act", "Set or clear a thread's short label.",
                         _schema({"thread_id": _t(), "label": {"type": ["string", "null"], "maxLength": LABEL_MAX}}, ("label",))),
    "read_memory": ("read", "Read this chat's memory: MEMORY.md (the index, the default) or one memory file it "
                    "lists. Returns the content and its version, which update_memory needs.",
                    _schema({"path": _t(FILE_NAME_MAX)})),
    "update_memory": ("act", "Replace one memory file whole (create it with if_version \"new\"), and keep MEMORY.md "
                      "as one short line per memory: - [Title](file.md) — hook. Save only what must outlive you: "
                      "the owner's durable preferences and decisions. Never secrets. A stale if_version returns the "
                      "current content: merge and write again.",
                      _schema({"path": _t(FILE_NAME_MAX), "content": _t(), "if_version": _t(MEMORY_VERSION_MAX)},
                              ("path", "content", "if_version"))),
    "ask_owner": ("act", "Ask the owner for a decision (approval), an answer (question) or something only they can "
                  "do (action). Returns at once with the item's id; the answer arrives later. With blocking false "
                  "(the default) keep working on your recommended option. Only a question takes options.",
                  _schema({"kind": {"enum": ["question", "approval", "action"]}, "title": _t(ASK_TITLE_MAX),
                           "why": _t(WHY_MAX),
                           "detail": _schema({"command": _t(), "diff": _t(), "url": _t(), "text": _t(),
                                              "paths": {"type": "array", "items": _S, "maxItems": MAX_PATHS}}),
                           "options": {"type": "array", "maxItems": MAX_OPTIONS, "description": "Only for a question.",
                                       "items": _schema({"id": _t(ID_MAX), "label": _t(WORD_MAX),
                                                         "consequence": _t()}, ("id", "label"))},
                           "recommended": _t(ID_MAX), "blocking": {"type": "boolean"}}, ("kind", "title", "why"))),
    "withdraw_ask": ("act", "Withdraw one of your own open asks that your work made moot.",
                     _schema({"review_id": _t(), "reason": _t(NOTE_MAX)}, ("review_id", "reason"))),
    "list_review": ("read", "This chat's items waiting on the owner.", _schema({"state": {"enum": ["open"]}})),
    "annotate_review": ("act", "Add one line of context under an item of this chat, labelled as your note. It never "
                        "decides the item.",
                        _schema({"review_id": _t(), "note": _t(NOTE_MAX)}, ("review_id", "note"))),
    "add_todo": ("act", "Add a todo to this project's list.",
                 _schema({"content": _t(SHORT_TEXT_MAX), "active_form": _t(SHORT_TEXT_MAX),
                          "description": _t(LONG_TEXT_MAX)}, ("content",))),
    "update_todo": ("act", "Change one of this project's todos: its status, content or active form.",
                    _schema({"todo_id": _t(ID_MAX),
                             "status": {"enum": ["pending", "in_progress", "completed", "cancelled",
                                                                "blocked"]},
                             "content": _t(SHORT_TEXT_MAX), "active_form": _t(SHORT_TEXT_MAX)}, ("todo_id",))),
    "list_todos": ("read", "This project's todos.", _schema({"status": _S})),
    "share_files": ("act", "Show the owner files from your working folder.",
                    _schema({"paths": {"type": "array", "items": _S, "maxItems": MAX_SHARED},
                             "caption": _t(CAPTION_MAX)},
                            ("paths",))),
}

_COORDINATOR = ("post_message", "react", "no_reply_needed", "fetch_timeline", "fetch_messages", "read_memory",
                "update_memory", "ask_owner", "withdraw_ask", "list_review", "annotate_review")
#: Each role's tools (ch 12 §3).
ROLE_TOOLS: Mapping[str, tuple[str, ...]] = {
    "workspace-coordinator": (*_COORDINATOR, "forward_to_project", "no_such_project", "propose_project",
                              "list_projects", "fetch_project_card"),
    "project-coordinator": (*_COORDINATOR, "start_thread_session", "message_thread", "ask_workspace",
                            "list_thread_sessions", "fetch_thread", "set_thread_resolved", "rename_thread",
                            "set_thread_label", "add_todo", "update_todo", "list_todos"),
    "thread": ("reply", "update_status", "react", "no_reply_needed", "message_coordinator", "fetch_thread",
               "fetch_timeline", "fetch_messages", "set_thread_resolved", "rename_thread", "set_thread_label",
               "read_memory", "ask_owner", "withdraw_ask", "add_todo", "update_todo", "list_todos", "share_files"),
    "plain": ("ask_owner", "withdraw_ask", "add_todo", "update_todo", "list_todos", "share_files"),
}


def kind(name: str) -> str:
    return TOOLS[name][0]


def terminal_tools(role: str) -> tuple[str, ...]:
    """The role's terminal tools by bare name; each runtime qualifies them its own way."""
    return tuple(name for name in ROLE_TOOLS[role] if kind(name) == "terminal")


#: The tools that choose a new thread's model and effort (§0.105).
CHOOSES_SETUP = ("start_thread_session",)
UNREAD_CATALOG = (" The catalog can't be read now, so it isn't listed here: leave model and effort out, and the "
                  "thread runs on its default.")


def tools_for(role: str, catalog: Catalog | None = None) -> list[dict[str, Any]]:
    """``tools/list`` for a role. A tool that chooses a new thread's model
    and effort states the thread agent's ``catalog``: its schema holds
    exactly the models and efforts its handler takes, and requires a model
    (and an effort, unless some model takes none). With no catalog (None:
    it can't be read now; or it lists no models) nothing can be chosen, so
    the tool says so and requires neither."""
    return [_listed(name, catalog) for name in ROLE_TOOLS[role]]


def _listed(name: str, catalog: Catalog | None) -> dict[str, Any]:
    _kind, description, schema = TOOLS[name]
    if name in CHOOSES_SETUP:
        if catalog is None or not catalog.models:
            description += UNREAD_CATALOG
        else:
            every_takes_effort = all(catalog.efforts(m.id) for m in catalog.models)
            description += (f" The catalog: {catalog.describe()}. model is required; effort too"
                            + ("." if every_takes_effort else ", unless the model takes none."))
            props = dict(schema["properties"])
            props["model"] = {**props["model"], "enum": [m.id for m in catalog.models]}
            if catalog.all_efforts():
                props["effort"] = {**props["effort"], "enum": list(catalog.all_efforts())}
            required = [*schema["required"], "model", *(("effort",) if every_takes_effort else ())]
            schema = {**schema, "properties": props, "required": required}
    return {"name": name, "description": description, "inputSchema": schema}


def endpoint_url() -> str:
    """The comms endpoint on this server, loopback only (ch 12 §2.2)."""
    return f"http://127.0.0.1:{os.getenv('PORT', '5002')}{PATH}"


def server_for(role: str) -> McpServer:
    """The comms mount a session of ``role`` gets: its token is the
    session's own, minted at each attach and carried only in the process's
    env (``bearer_env``); the spec never holds it. Its tools need no
    permission, and the role's terminal tools end a turn."""
    return McpServer(name=SERVER_NAME, transport="http", url=endpoint_url(), bearer_env=SESSION_TOKEN_ENV,
                     auto_approve=True, terminal_tools=terminal_tools(role))
