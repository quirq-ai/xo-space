"""The sessions module's facade: what the routes, the stream, the CLI
commands and other modules call.

A session is one conversation with an agent. The Space knows it by its
index row (the adapter's shard under ``projects/<pid>/sessions/
sessionslist.d/``, plus the ``purpose`` this module stamps), reads its
record through the owning adapter's ``sessions`` capability and drives it
through the agent engine's dispatcher.

Reads, what the routes used to do themselves: :func:`list_sessions`,
:func:`search`, :func:`get`, :func:`messages`, :func:`transcript`,
:func:`todos`, :func:`files`, :func:`backend_of`. Writes: :func:`create`,
:func:`update`, :func:`delete`, :func:`record_purpose`. The one new thing
is :func:`start`: it runs the active agent's stream for a purpose (an
inbox item, a job, a workitem, a chat), writes or updates the session's
index row with that purpose next to the adapter's fields, emits
``session.started`` once through ``modules.timeline.service`` (filed under
the project's runtime key) and raises the ``sessions.started`` signal, and
yields the same events the dispatcher yields. The chat routes keep their
own streaming path and call :func:`record_purpose` with ``"chat"`` on the
rows they create.

Knows nothing about HTTP: a failure is a :class:`ServiceError` carrying its
status (:class:`SessionNotFound` answers the bare ``{"detail": "Session not
found"}`` these routes have always given). Names no agent: the backend is
whatever ``resolve_agent_name`` or the session's own index row says, and
everything agent-specific is reached through the capability loader.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import modules.timeline.service as timeline_service
from services import signals
from services.cowork_agent import project_layout
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.registry.adapter_registry import list_adapters
from services.errors import ServiceError
from services.timestamps import now_iso
from services.xo_manifest import resolve_agent_name

from . import events, session_transcript, sessions_io
from .session_transcript import SessionNotFound  # noqa: F401  (re-exported: the routes' 404)
from .sessions_io import find_session_backend, load_all_sessions

logger = logging.getLogger(__name__)

#: The purposes the Space knows by name; any other short word is accepted.
PURPOSES = ("chat", "inbox_item", "job", "workitem")
PURPOSE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
#: The runtime home's folder name when the project has a pid (the watcher's
#: sink files lines the same way; a project without one is keyed by name).
_PID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: The composite key of a row this module writes when no adapter wrote one.
_OWN_KEY = "sessions:{backend}:{session_id}"

LIMIT_DEFAULT = 50
SEARCH_LIMIT_DEFAULT = 20


# ── Reads ────────────────────────────────────────────────────────────────────


def list_sessions(limit: int = LIMIT_DEFAULT, offset: int = 0) -> list[dict]:
    """Every session the active backend knows, newest first, one page."""
    return load_all_sessions()[offset: offset + limit]


def search(q: str = "", limit: int = SEARCH_LIMIT_DEFAULT, offset: int = 0) -> list[dict]:
    """Sessions whose title contains ``q`` (case-insensitive), as
    ``{"session", "snippet"}`` rows; ``snippet`` is always ``None``."""
    needle = (q or "").lower()
    results = [{"session": s, "snippet": None}
               for s in load_all_sessions() if needle in (s.get("title") or "").lower()]
    return results[offset: offset + limit]


def get(session_id: str) -> dict:
    """One session by id; :class:`SessionNotFound` otherwise."""
    for s in load_all_sessions():
        if s.get("id") == session_id:
            return s
    raise SessionNotFound(session_id)


def backend_of(session_id: str) -> Optional[str]:
    """The adapter that owns ``session_id`` (its index row's ``backend``
    tag, or the adapter whose native store claims it), or ``None``."""
    return find_session_backend(session_id)


def messages(session_id: str, limit: int = 50, offset: int = -1) -> dict:
    """``{"total", "offset", "messages"}`` from the owning adapter's
    ``get_messages``. ``offset=-1`` means the latest page and answers every
    message: the old ``all[total - limit:]`` slid forward as ``total`` grew
    between polls and dropped messages that were visible a moment ago; the
    conversation length is bounded by the model's context in practice."""
    backend = find_session_backend(session_id)
    mod = try_load_capability("sessions", agent=backend) if backend else None
    fn = getattr(mod, "get_messages", None) if mod else None
    all_messages = fn(session_id) if fn else []
    total = len(all_messages)
    if offset == -1:
        start_at, page = 0, all_messages
    else:
        start_at, page = offset, all_messages[offset: offset + limit]
    return {"total": total, "offset": start_at, "messages": page}


def transcript(session_id: str, *, include_tools: bool = False) -> dict:
    """``{title, messages: [{id, role, content}]}``, one text bubble per
    turn (:mod:`session_transcript`); :class:`SessionNotFound` otherwise."""
    return session_transcript.load_transcript(session_id, include_tools=include_tools)


def todos(session_id: str) -> dict:
    """Per-session todos live in the project's ``todos.json`` (the projects
    module); this surface has always answered an empty list."""
    return {"todos": []}


def files(session_id: str) -> dict:
    return {"files": []}


# ── Writes ───────────────────────────────────────────────────────────────────


def create() -> dict:
    """A fresh id and title. Sessions come into being through the chat
    prompt (or :func:`start`); this only hands out the id the surface asks
    for first."""
    return {"id": str(uuid.uuid4()), "title": "New Chat"}


def _parse_body(body: Any) -> dict:
    """A request body as a dict: bytes or text are parsed as JSON and read
    as ``{}`` when they are not (as the route always did), a dict is taken
    as is, anything else is ``{}``."""
    if isinstance(body, dict):
        return body
    if isinstance(body, (bytes, bytearray, str)):
        try:
            parsed = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def update(session_id: str, body: Any = None) -> dict:
    """``PATCH``: set the session's working directory. A body without
    ``directory`` changes nothing (``{"ok": true}``); an empty one is a 400;
    the owning adapter's ``set_session_directory`` answers, and no adapter
    claiming the session is a 404. ``body`` may be the raw request body."""
    data = _parse_body(body)
    directory = data.get("directory")
    if directory is None:
        return {"ok": True}
    directory = str(directory).strip()
    if not directory:
        raise ServiceError(None, "directory must be a non-empty string", 400)
    for name in list_adapters():
        mod = try_load_capability("sessions", agent=name)
        fn = getattr(mod, "set_session_directory", None) if mod else None
        if fn is None:
            continue
        result = fn(session_id, directory)
        if result is not None:
            return result
    raise SessionNotFound(session_id)


def delete(session_id: str) -> dict:
    """The record stays (the agent's store owns it); this has always
    answered ``{"ok": true}``."""
    return {"ok": True}


def check_purpose(purpose: Any) -> str:
    """A purpose is a short word (``chat``, ``inbox_item``, ``job``,
    ``workitem``, or any ``[a-z][a-z0-9_-]{0,31}``)."""
    if not isinstance(purpose, str) or PURPOSE_RE.fullmatch(purpose) is None:
        raise ServiceError("invalid_purpose",
                           f"purpose must be a short word such as {', '.join(PURPOSES)} (got {purpose!r}).")
    return purpose


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def record_purpose(session_id: str, purpose: str, *, overwrite: bool = True, create: bool = False,
                   project_id: Optional[str] = None, backend: Optional[str] = None) -> bool:
    """Stamp ``purpose`` on the session's index row, next to the adapter's
    fields. With ``overwrite`` false a row that already has a purpose keeps
    it. With ``create`` and a ``project_id``, a session no adapter wrote a
    row for gets a minimal one under this module's own key (``sessionId``,
    ``backend``, ``directory``, ``updatedAt``, ``purpose``). Returns whether
    the row carries the purpose afterwards; never raises for a missing row
    or a disk fault (logged), so a stream is never failed by bookkeeping."""
    purpose = check_purpose(purpose)
    try:
        found = sessions_io.find_session_row(session_id)
        if found is not None:
            project, key, row = found
            current = row.get("purpose")
            if current == purpose or (not overwrite and isinstance(current, str) and current):
                return True
            row["purpose"] = purpose
            return sessions_io.write_session_row(project, key, row)
        if not create or not project_id:
            return False
        row = {
            "sessionId": session_id,
            "nativeSessionId": "",
            "directory": str(project_layout.project_dir(project_id)),
            "backend": backend or "",
            "updatedAt": _now_ms(),
            "purpose": purpose,
        }
        key = _OWN_KEY.format(backend=backend or "agent", session_id=session_id)
        return sessions_io.write_session_row(project_id, key, row)
    except OSError as exc:
        logger.warning("sessions: could not record purpose %r on %s: %s", purpose, session_id, exc)
        return False


# ── start(): a session for a purpose ─────────────────────────────────────────


def _dispatcher(agent_name: str):
    """The engine's dispatcher for ``agent_name`` (imported here, not at
    load: the registry pulls the adapters in). Tests patch this."""
    from services.cowork_agent.engine.dispatcher import AgentDispatcher

    return AgentDispatcher(agent_name)


def _timeline_target(project_id: Optional[str]) -> dict:
    """Where the project's lines are filed: ``{"pid": ...}`` for a project
    with an identity, ``{"key": ...}`` for one keyed by its folder name,
    ``{}`` (the Space log) for no or an unknown project."""
    if not project_id:
        return {}
    root = project_layout.runtime_dir_for_project(project_id)
    if root is None:
        return {}
    if _PID_RE.fullmatch(root.name):
        return {"pid": root.name}
    return {"key": root.name}


def announce_started(session_id: str, *, purpose: str, project_id: Optional[str], runtime: str) -> list[dict]:
    """Write the ``session.started`` line once (the project's log when the
    project is known, else the Space log) and return what was written."""
    line = {"ts": now_iso(), "type": events.TYPES[0], "session_id": session_id,
            "runtime": runtime, "purpose": purpose}
    return timeline_service.emit([line], project_id=project_id or None, **_timeline_target(project_id))


def start(prompt: str, *, purpose: str, project_id: Optional[str] = None,
          session_id: Optional[str] = None, agent_type: Optional[str] = None,
          user_id: Optional[str] = None) -> AsyncIterator[dict]:
    """Run one turn of a session for ``purpose`` and yield the dispatcher's
    events (``{"type": "token", ...}``, ``{"type": "error", ...}``, ...,
    then ``{"done": True, "native_session_id": ...}``).

    Without ``session_id`` a new session starts (a fresh id, the active
    agent); with one, that session's own backend continues it. ``project_id``
    is the project folder the agent works in (the adapters' ``agent_id``)
    and the project whose timeline takes the ``session.started`` line, which
    is written once, when the stream is first pulled. The purpose is stamped
    on the session's index row after the first event (the adapter has
    written its row by then) and again when the stream ends; a session no
    adapter wrote a row for gets a minimal row of this module's own.
    The dispatcher is built now, so an agent with no adapter fails here,
    not inside the stream."""
    text = (prompt or "").strip()
    if not text:
        raise ServiceError("empty_prompt", "The prompt is empty.")
    purpose = check_purpose(purpose)
    is_new = not session_id
    if is_new:
        backend = resolve_agent_name()
        sid = str(uuid.uuid4())
    else:
        sid = str(session_id)
        backend = find_session_backend(sid) or resolve_agent_name()
    dispatcher = _dispatcher(backend)

    async def run() -> AsyncIterator[dict]:
        try:
            announce_started(sid, purpose=purpose, project_id=project_id, runtime=backend)
        except OSError as exc:
            # The session runs even when its timeline cannot be written.
            logger.warning("sessions: could not write session.started for %s: %s", sid, exc)
        await signals.notify(f"sessions.{events.SIGNALS[0]}", session_id=sid, purpose=purpose,
                             project_id=project_id, runtime=backend)
        stamped = False
        try:
            async for event in dispatcher.stream(
                text, None, agent_type=agent_type, our_session_id=sid, agent_id=project_id,
                is_new_session=is_new, user_id=user_id,
            ):
                if not stamped:
                    stamped = record_purpose(sid, purpose, project_id=project_id, backend=backend)
                yield event
        finally:
            record_purpose(sid, purpose, create=True, project_id=project_id, backend=backend)

    return run()
