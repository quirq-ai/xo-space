"""The ``xo_comms`` tools: each one's schema and kind, and which roles have
it (design ch 12 §3–§5, §0.54). Pure data, imported by the presets' callers
(the comms mount a session gets) and by the endpoint (``tools/list``).

The token decides the caller's role and the role decides its tools: a role
never sees, and can never call, another's (ch 12 §2.2, §6). The lines of
talk (ch 11 §16.1) are this table, ``ASK_TO`` and the handlers' scope
checks: a thread asks the founder or its own coordinator (no address) and
reaches neither the workspace nor another thread; only a project
coordinator asks the workspace; the workspace coordinator reaches a thread
only through a project (``forward_to_project``).

**A question wakes someone; a report doesn't** (w5-w7-design W5): ``ask``
wakes whom it names, ``report`` and ``submit_result`` land on the thread's
task, and the coordinator reads them at its next real turn. **Terminal**
tools end the agent's work properly; **routing** tools hand it on. Both
satisfy the reply gate (ch 11 §5); so does an ``ask`` that hands work on (to
the coordinator or the workspace) or blocks on the founder, which the gate
reads from the call. Every role has ``read_state`` and the job tools
(``create_job``, ``list_jobs``, ``cancel_job``, ch 12 §5.8); no job tool is
terminal: a job is only asked for, and the agent still answers.

A tool this build replaced (``REPLACED``) is refused with its replacement's
name, so a session that resumes from before the upgrade adapts in one step.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from services.cowork_agent.adapters.contract import McpServer
from services.cowork_agent.storm.engine.model import SESSION_TOKEN_ENV
from services.cowork_agent.storm.engine.catalog import EFFORTS, Catalog
from utils.commands import scheduler

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
SHARE_TITLE_MAX = 80            # a shared file's title, shown in place of its file name
#: What an ask's title is: the owner reads it as the headline.
ASK_TITLE_HINT = ("In plain words: what will happen and why. Put an exact command or paths in detail.command / "
                  "detail.paths, not in the title.")
MEMORY_VERSION_MAX = 12         # chats.memory.VERSION_HEX (a test keeps them equal)
TIME_MAX = 64                   # an ISO-8601 time

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
                           "touches several. With only a note, it answers that project coordinator's question. "
                           "ask_id is the hop id of a project's ask you are serving: to the asking project it "
                           "answers the ask; to another project it hands the ask's work on, with the owner's "
                           "messages the ask carried in context_message_ids, and XO tells the asking project.",
                           _schema({"project": _t(), "note": _t(), "context_message_ids": _IDS,
                                    "ask_id": _t(ID_MAX)}, ("project",))),
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
    "create_task": ("routing", "Start a task: a thread, a separate agent session with its own context, in its own "
                    "working folder of this project, anchored on a message of this chat's timeline (message_id), "
                    "and the task XO records for it: the owner's when the anchor is the owner's own message, yours "
                    "otherwise. The server copies the anchor and context_message_ids verbatim into the thread. instructions is your brief, kept as the task's instructions: add only what the "
                    "thread needs beyond the owner's words. goal_id links it to a goal you set with set_goal. ack "
                    "is the thread's first line; project_ack is one short, varied line in this chat. Choose the "
                    "thread's model and effort for this work from the thread agent's catalog: high effort for "
                    "hard work, lower for a small edit. A choice the catalog lacks isn't used: the thread then "
                    "runs on its default, and the result's note says why.",
                    _schema({"message_id": _t(), "title": _t(TITLE_MAX), "instructions": _t(),
                             "context_message_ids": _IDS, "ack": _t(LINE_MAX), "project_ack": _t(SHORT_LINE_MAX),
                             "model": _t(WORD_MAX), "effort": _t(WORD_MAX), "agent": _t(WORD_MAX),
                             "goal_id": _t(ID_MAX)},
                            ("message_id", "title", "instructions"))),
    "message_thread": ("routing", "Send a follow-up to an existing thread (thread_id = the thread's root message id): "
                       "the owner's messages in context_message_ids, your note, or both. It reopens a resolved "
                       "thread, and answers the thread's own question when it asked you one. delivery, while the "
                       "thread is working: next (the default) waits for its current turn to end; steer joins the "
                       "running turn (extra detail, a small correction: the work in progress stays useful); "
                       "interrupt stops the running turn first (the work in progress is wrong, or the owner changed "
                       "course). A turn the owner's own message started can be steered, but interrupted only when "
                       "you cite a newer owner message. If interrupting would stop background sub-agents, the call "
                       "is refused with their count: call again with confirm_stop_runs set to it. Then post one "
                       "line saying where it went.",
                       _schema({"thread_id": _t(), "note": _t(), "context_message_ids": _IDS,
                                "delivery": {"enum": ["next", "steer", "interrupt"]},
                                "confirm_stop_runs": {"type": "integer", "minimum": 1}}, ("thread_id",))),
    "continue_thread": ("routing", "Move a thread's work to a new thread, usually on another runtime: XO stops the "
                        "thread, commits its work in progress, freezes it (the owner can pick it back up), and "
                        "starts a linked thread from that commit with a handoff XO builds. The new thread's first "
                        "turn is a checkpoint the owner confirms. Use it when the thread's runtime can't take work "
                        "now (its usage limit, a lost login); for any other reason, only when the owner asked "
                        "(cite their message in context_message_ids). It sends the thread's content to the new "
                        "runtime's provider.",
                        _schema({"thread_id": _t(), "agent": _t(WORD_MAX), "reason": _t(WORD_MAX),
                                 "model": _t(WORD_MAX), "effort": _t(WORD_MAX), "instructions": _t(),
                                 "context_message_ids": _IDS}, ("thread_id", "reason"))),
    "stop_thread": ("act", "Stop a thread's running turn and its sub-agents, with nothing sent after it: for work "
                    "that is abandoned or moving elsewhere. The thread keeps its session and can be resumed with "
                    "message_thread. The thread shows that you stopped it and your reason. A turn the owner's own "
                    "message started is stopped only when you cite a newer owner message in context_message_ids.",
                    _schema({"thread_id": _t(), "reason": _t(WORD_MAX), "context_message_ids": _IDS},
                            ("thread_id", "reason"))),
    "report": ("terminal", "Report progress on your task: XO keeps it as the task's latest report, which your "
               "coordinator reads at its next turn and in read_state. It wakes no one; when you need an answer, use "
               "ask.", _schema({"text": _t(LONG_TEXT_MAX)}, ("text",))),
    "submit_result": ("terminal", "Record what you delivered for your task, as you report it: a summary, and its "
                      "artifacts, each a file in your working folder (path) or a commit (commit: a ref such as "
                      "HEAD or a branch). XO takes each file's sha256 and each commit's sha itself, records them "
                      "with your agent, model and cost, and marks your task done. It wakes no one: reply in your "
                      "thread so the owner sees the result.",
                      _schema({"summary": _t(LONG_TEXT_MAX),
                               "artifacts": {"type": "array", "maxItems": MAX_SHARED, "items": _schema(
                                   {"path": _t(TEXT_MAX), "commit": _t(WORD_MAX)})}}, ("summary",))),
    "ask": ("act", "Ask someone, which wakes them. to \"founder\" (the owner): a decision (approval), an answer "
            "(question) or something only they can do (action), with kind, title, why and detail; it returns at "
            "once with the item's id, and the answer arrives later as a new wake. With blocking false (the "
            "default) keep working on your recommended option; only a question takes options. to "
            "\"coordinator\" (a thread): a question or a hand-off your project coordinator must act on, in "
            "question; its answer arrives as a relay. to \"workspace\" (a project coordinator): anything outside "
            "this project, or work for another project, in question; on_behalf_of names the thread that asked "
            "you; the answer arrives later as a relay, so end your turn. context_message_ids are owner messages "
            "the server copies verbatim for the coordinator or the workspace. For progress that needs no answer, "
            "use report: it wakes no one.",
            _schema({"to": {"enum": ["founder", "coordinator", "workspace"]},
                     "question": _t(), "context_message_ids": _IDS, "on_behalf_of": _t(),
                     "kind": {"enum": ["question", "approval", "action"]},
                     "title": {**_t(ASK_TITLE_MAX), "description": ASK_TITLE_HINT},
                     "why": _t(WHY_MAX),
                     "detail": _schema({"command": _t(), "diff": _t(), "url": _t(), "text": _t(),
                                        "paths": {"type": "array", "items": _S, "maxItems": MAX_PATHS}}),
                     "options": {"type": "array", "maxItems": MAX_OPTIONS, "description": "Only for a question.",
                                 "items": _schema({"id": _t(ID_MAX), "label": _t(WORD_MAX),
                                                   "consequence": _t()}, ("id", "label"))},
                     "recommended": _t(ID_MAX), "blocking": {"type": "boolean"}}, ("to",))),
    "set_goal": ("act", "Record the owner's goal and when it ends (ends_at, ISO-8601 with a UTC offset, in the "
                 "future), citing the owner's own message that asks for it (message_id): the server checks it is "
                 "theirs, in this chat. title is the goal in your words. It returns the goal's id, for create_task's "
                 "goal_id. To close a goal as you report it, pass only goal_id, state (done or failed) and reason.",
                 _schema({"message_id": _t(), "title": _t(TITLE_MAX), "ends_at": _t(TIME_MAX),
                          "goal_id": _t(ID_MAX), "state": {"enum": ["done", "failed"]}, "reason": _t(WORD_MAX)})),
    "read_state": ("read", "XO's facts as of now: the Space advisory (each account's usage windows, the machine "
                   "and the sessions, each fact with its source and age), your goals, your tasks with their latest "
                   "reports and results, your open questions, and for a coordinator this chat's items waiting on "
                   "the owner.", _schema({})),
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
    "create_job": ("act", "Ask the owner to schedule a job: an agent run in a project, each run a new thread there "
                   "with prompt as its task. It repeats every every.seconds from first_run_at (ISO-8601 with a UTC "
                   "offset), or runs once with every null. Calendar rules (weekdays only, month ends) can't be "
                   "expressed: every Monday at 19:00 is 604800 seconds from a Monday 19:00. project defaults to "
                   "yours; name it when you have none. title and prompt are stored and shown, so one holding a "
                   "secret is refused: name its secrets-store entry instead. Nothing is scheduled until the owner "
                   "allows it; it returns the item's id, and their answer arrives as a new wake.",
                   _schema({"title": {"type": "string", "maxLength": scheduler.NAME_MAX,
                                      "pattern": scheduler.NAME_PATTERN},
                            "prompt": _t(scheduler.PROMPT_MAX),
                            "every": {"type": ["object", "null"], "additionalProperties": False,
                                      "required": ["seconds"],
                                      "properties": {"seconds": {"type": "integer",
                                                                 "minimum": scheduler.min_every_seconds()}}},
                            "first_run_at": _t(TIME_MAX), "project": _t(ID_MAX), "agent": _t(WORD_MAX),
                            "model": _t(WORD_MAX), "effort": {"enum": list(EFFORTS)}},
                           ("title", "prompt", "every", "first_run_at"))),
    "list_jobs": ("read", "The agent jobs of a project (yours by default; every project's when you have none): "
                  "their schedule, next run, last run and whether they are on.", _schema({"project": _t(ID_MAX)})),
    "cancel_job": ("act", "Turn a job off at once; no approval needed. reason says why. The owner can turn it back "
                   "on.", _schema({"job_id": _t(ID_MAX), "reason": _t(NOTE_MAX)}, ("job_id", "reason"))),
    "share_files": ("act", "Show the owner files from your working folder. Pictures (PNG, JPEG, WebP, GIF) appear "
                    "in the conversation as pictures, several as an album; other files as cards. Share the finished "
                    "result rather than every draft, say in the caption what the pictures show, and give each file "
                    "a short title in plain words (titles[i] names paths[i]): it is what the owner sees in place of "
                    "the file name.",
                    _schema({"paths": {"type": "array", "items": _S, "maxItems": MAX_SHARED},
                             "titles": {"type": "array", "items": _t(SHARE_TITLE_MAX), "maxItems": MAX_SHARED,
                                        "description": "One per path, in the same order; optional."},
                             "caption": _t(CAPTION_MAX)},
                            ("paths",))),
}

_JOBS = ("create_job", "list_jobs", "cancel_job")
_COORDINATOR = ("post_message", "react", "no_reply_needed", "fetch_timeline", "fetch_messages", "read_memory",
                "update_memory", "ask", "withdraw_ask", "list_review", "annotate_review", "read_state", *_JOBS)
#: Each role's tools (ch 12 §3; w5-w7-design W5).
ROLE_TOOLS: Mapping[str, tuple[str, ...]] = {
    "workspace-coordinator": (*_COORDINATOR, "forward_to_project", "no_such_project", "propose_project",
                              "list_projects", "fetch_project_card"),
    "project-coordinator": (*_COORDINATOR, "set_goal", "create_task", "message_thread", "stop_thread",
                            "continue_thread", "list_thread_sessions", "fetch_thread", "set_thread_resolved",
                            "rename_thread", "set_thread_label", "add_todo", "update_todo", "list_todos"),
    "thread": ("reply", "update_status", "report", "submit_result", "react", "no_reply_needed", "ask", "fetch_thread",
               "fetch_timeline", "fetch_messages", "set_thread_resolved", "rename_thread", "set_thread_label",
               "read_memory", "withdraw_ask", "add_todo", "update_todo", "list_todos", "share_files", "read_state",
               *_JOBS),
    "plain": ("ask", "withdraw_ask", "add_todo", "update_todo", "list_todos", "share_files", "read_state", *_JOBS),
}
#: Whom each role's ``ask`` reaches: the founder, and the one above it.
ASK_TO: Mapping[str, tuple[str, ...]] = {
    "workspace-coordinator": ("founder",), "project-coordinator": ("founder", "workspace"),
    "thread": ("founder", "coordinator"), "plain": ("founder",)}
#: The tools this build replaced, each with its replacement and how to use it.
REPLACED: Mapping[str, tuple[str, str]] = {
    "start_thread_session": ("create_task", "create_task, with the same arguments"),
    "message_coordinator": ("ask", "report for progress (it wakes no one), or ask with to \"coordinator\" and "
                                   "question for what your coordinator must answer"),
    "ask_owner": ("ask", "ask with to \"founder\" and the same fields"),
    "ask_workspace": ("ask", "ask with to \"workspace\" and the same fields"),
}


def kind(name: str) -> str:
    return TOOLS[name][0]


def unknown_tool(name: object, role: str) -> str:
    """The refusal of a tool ``role`` doesn't have; one this build replaced
    names what to use instead."""
    if isinstance(name, str) and name in REPLACED and REPLACED[name][0] in ROLE_TOOLS[role]:
        return f"{name!r} was replaced: use {REPLACED[name][1]}."
    return f"{name!r} is not a tool for a {role}."


def terminal_tools(role: str) -> tuple[str, ...]:
    """The role's terminal tools by bare name; each runtime qualifies them its own way."""
    return tuple(name for name in ROLE_TOOLS[role] if kind(name) == "terminal")


#: The tools that choose a new thread's model and effort (§0.105).
CHOOSES_SETUP = ("create_task",)
UNREAD_CATALOG = (" The catalog can't be read now, so it isn't listed here: leave model and effort out, and the "
                  "thread runs on its default.")


def tools_for(role: str, catalog: Catalog | None = None, *,
              runtimes: list[tuple[str, Catalog | None]] | None = None,
              preferences: str | None = None) -> list[dict[str, Any]]:
    """``tools/list`` for a role. A tool that chooses a new thread's model
    and effort states the thread agent's ``catalog``: its schema holds
    exactly the models and efforts its handler takes, and requires a model
    (and an effort, unless some model takes none). With no catalog (None:
    it can't be read now; or it lists no models) nothing can be chosen, so
    the tool says so and requires neither. With several ``runtimes`` the
    thread may run on (the first is the default), the tool offers each with
    its catalog and the owner's ``preferences``, and takes ``agent``."""
    if runtimes and len(runtimes) > 1:
        return [_listed_runtimes(name, runtimes, preferences) if name in CHOOSES_SETUP else _listed(name, None, role)
                for name in ROLE_TOOLS[role]]
    return [_listed(name, catalog, role) for name in ROLE_TOOLS[role]]


def _listed_runtimes(name: str, runtimes: list[tuple[str, Catalog | None]], preferences: str | None
                     ) -> dict[str, Any]:
    """A thread-starting tool over several runtimes: ``agent`` names one (the
    first when left out), and model and effort come from that one's catalog."""
    _kind, description, schema = TOOLS[name]
    listed = [(agent, cat) for agent, cat in runtimes]
    parts = []
    for i, (agent, cat) in enumerate(listed):
        what = cat.describe() if cat is not None and cat.models else "its catalog can't be read now (leave model " \
                                                                       "and effort out for it)"
        parts.append(f"{agent}{' (the default)' if i == 0 else ''}: {what}")
    description += (" Choose the thread's runtime (agent) for this work, then a model and effort from that "
                    "runtime's catalog. The runtimes it may run on now: " + "; ".join(parts) + "."
                    + (f" The owner's preferences: {preferences}" if preferences else ""))
    readable = [cat for _a, cat in listed if cat is not None and cat.models]
    props = dict(schema["properties"])
    props["agent"] = {"type": "string", "enum": [a for a, _c in listed]}
    required = list(schema["required"])
    if readable:
        props["model"] = {**props["model"], "enum": list(dict.fromkeys(m.id for c in readable for m in c.models))}
        efforts = [e for e in EFFORTS if any(e in c.all_efforts() for c in readable)]
        if efforts:
            props["effort"] = {**props["effort"], "enum": efforts}
        if len(readable) == len(listed):
            required.append("model")
    return {"name": name, "description": description, "inputSchema": {**schema, "properties": props,
                                                                      "required": required}}


def _listed(name: str, catalog: Catalog | None, role: str) -> dict[str, Any]:
    _kind, description, schema = TOOLS[name]
    if name == "ask":                       # only whom this role reaches
        schema = {**schema, "properties": {**schema["properties"], "to": {"enum": list(ASK_TO[role])}}}
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
