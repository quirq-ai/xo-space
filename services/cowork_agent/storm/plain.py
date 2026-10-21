"""Plain chat on storm: what ``POST /api/chat/prompt``, ``/api/chat/abort``
and ``GET /api/chat/active`` do (design ch 17 §2.1, §3; ch 10 §4.1; owner
§0.108, §0.109; sedc 09 #5, #6, #12).

**Where a prompt goes** (``route``), read from the raw body before any model
validates it, so a caller of an agent without a runtime keeps today's body:

1. a ``session_id`` storm knows (its ``cse_…`` id, or a storm session's
   ``native_id``): that session;
2. else a ``session_id`` today's chat path knows (XO's session index, or an
   adapter's own store): that chat's agent;
3. else the body's ``agent`` (storm's field) or ``agent_name`` (today's),
   else the Space's agent.

An agent with a storm runtime is storm's; any other keeps today's chat path
exactly (told the agent this routing chose, and, for a storm session whose
body names an agent without a runtime, given no session: today's agent
switch starts a fresh chat), unless it has no chat of its own either (an adapter whose plain
chat is waiting for its runtime): that is refused up front, ``501
unsupported`` with feature ``chat``.

**On storm** a prompt without ``session_id`` creates a plain session (preset
``plain_chat``) and delivers the text as the person's message; with one it
delivers to that session (``delivery`` ``add`` or ``now``; ``now`` over live
sub-agents is ``409 would_stop_runs`` until confirmed), resuming it if the
loop breaker paused it. A pre-switch chat id (§0.109) opens a plain session
that resumes that chat's CLI transcript in its folder, found server-side in
XO's session index: never a native id taken from the request. ``client_id``
makes a retry deliver once. The answer is ``{session_id, turn_id}``:
``turn_id`` is the turn that took the message, null while it waits for one
(the session's stream says when it starts; a retry answers it).
"""

from __future__ import annotations

import asyncio
import os
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.engine import sessions_io
from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm import presets
from services.cowork_agent.storm.comms.tools import SERVER_NAME as COMMS_SERVER
from services.cowork_agent.storm.comms.tools import server_for
from services.cowork_agent.storm.engine import Engine, EngineUnavailable, SessionNotFound, Unsupported, Wake, Writes
from services.cowork_agent.storm.engine.specs import spec_from_json
from services.cowork_agent.storm.presets import Intelligence, PresetContext
from services.errors import ServiceError
from services.xo_manifest import resolve_agent_name

PRESET = "plain_chat"
ROLE = presets.ROLES[PRESET]
#: Who an abort from the person names.
PERSON = "person"
#: A plain chat's message wake: ``msg:`` marks the person's own message (it
#: moves a session on from a parked decision), and the client's id makes a
#: retry the same wake.
WAKE_ID = "msg:prompt:{client_id}"


class PlainRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(code, message, status=status)


@dataclass(frozen=True)
class Session:
    """A storm session as plain chat reads it."""

    id: str
    agent: str
    role: str
    cwd: str
    comms: bool
    model: str | None
    effort: str | None
    permission_mode: str | None
    paused: str | None


@dataclass(frozen=True)
class Adopted:
    """A chat today's ``/api/chat`` started (§0.109), as XO's session index has it."""

    chat_id: str
    native_id: str
    cwd: str


@dataclass(frozen=True)
class Route:
    """Where a prompt goes. ``storm`` False: today's chat path, with the raw
    body and ``today`` over it. Otherwise ``agent``'s plain chat, continuing
    ``session`` or resuming ``adopted``, or a new one."""

    storm: bool
    agent: str | None = None
    session: Session | None = None
    adopted: Adopted | None = None
    today: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Prompt:
    """``POST /api/chat/prompt``'s body on storm (ch 17 §2.1)."""

    text: str
    client_id: str
    session_id: str | None = None
    agent: str | None = None
    model: str | None = None
    effort: c.Effort | None = None
    permission_mode: c.PermissionMode | None = None
    comms: bool | None = None
    cwd: str | None = None
    delivery: Literal["add", "now"] | None = None
    confirm_stop_runs: int | None = None


def running() -> Engine | None:
    """The engine, or None when this server runs none (a route to storm
    then answers its 503)."""
    try:
        return storm_engine.current()
    except EngineUnavailable:
        return None


async def route(body: Any) -> Route:
    if not isinstance(body, dict):
        return Route(storm=False)          # today's path answers it as it always has
    engine = running()
    session_id = _text(body.get("session_id"))
    named = _text(body.get("agent")) or _text(body.get("agent_name"))
    known = await find(engine, session_id) if session_id and engine is not None else None
    if known is not None and (named is None or _has_runtime(engine, named)):
        return Route(storm=True, agent=known.agent, session=known)
    owner = await asyncio.to_thread(sessions_io.find_session_backend, session_id) \
        if session_id and known is None else None
    agent = named or owner or resolve_agent_name()
    if not _has_runtime(engine, agent):
        if _offers_no_chat(agent):
            raise Unsupported("chat", f"{agent} has no storm runtime and no chat of its own, so it can't chat here "
                                      "yet; chat with another agent.")
        today: dict[str, Any] = {}
        if known is not None:
            today["session_id"] = None       # a storm chat, and the person switched agent: a fresh chat, as today
        if _text(body.get("agent")) and not _text(body.get("agent_name")):
            today["agent_name"] = agent      # storm's field; today's path reads only agent_name
        return Route(storm=False, today=today)
    if engine is None:
        storm_engine.current()              # raises why this server runs no engine (503)
    if not session_id:
        return Route(storm=True, agent=agent)
    if owner is not None and owner != agent:
        raise PlainRefused("other_agents_chat", f"Chat {session_id} is {owner}'s, not {agent}'s. Leave out "
                                                f"session_id to start a new chat on {agent}.")
    row = await asyncio.to_thread(_index_row, session_id)
    if row is None or not row.get("nativeSessionId"):
        raise SessionNotFound(session_id)
    return Route(storm=True, agent=agent, adopted=Adopted(session_id, row["nativeSessionId"], row["directory"]))


async def prompt(engine: Engine, to: Route, req: Prompt) -> dict[str, Any]:
    wake = Wake(id=WAKE_ID.format(client_id=req.client_id), text=req.text, priority="now", reason="message",
                origin="person", delivery=req.delivery)
    if (retried := await _delivered(engine, wake.id)) is not None:
        return _answer(*_same_target(retried, to))
    session = to.session
    if session is None and to.adopted is not None:
        session = await find(engine, to.adopted.native_id)       # resumed on storm already
    if session is None:
        return _answer(*await _start(engine, to, req, wake))
    if session.role != ROLE:
        raise PlainRefused("not_plain_chat", f"Session {session.id} is a {session.role}'s; write to it in its chat "
                                             "(POST /api/chat/chats/{chat}/messages).", status=409)
    if req.agent is not None and req.agent != session.agent:
        raise PlainRefused("agent_locked", f"Session {session.id} runs on {session.agent} for life; leave out agent, "
                                           "or start a new chat for another agent.", status=409)
    await _settings_fixed(session, req)
    await _update(engine, session, req)
    await engine.deliver(session.id, wake, confirm_stop_runs=req.confirm_stop_runs)
    if session.paused:
        await engine.resume(session.id)        # the person's message lifts the loop breaker's pause (sedc 09 #12)
    retried = await _delivered(engine, wake.id)
    return _answer(*_same_target(retried, Route(storm=True, session=session)))


async def abort(engine: Engine, session_id: str) -> dict[str, Any]:
    """Stop the session's running turn and its sub-agents now (ch 17 §2.1;
    the 3-second Undo is the UI's, sedc 09 #1)."""
    session = await find(engine, session_id)
    if session is None:
        raise SessionNotFound(session_id)
    result = await engine.stop(session.id, by=PERSON)
    return {"session_id": session.id, "stopped": result.stopped, "turn_id": result.turn_id}


async def active(engine: Engine) -> dict[str, Any]:
    """The turns running now, one per session (ch 17 §2.1)."""
    rows = await engine.store.read(lambda conn: conn.execute(
        "SELECT t.session_id, s.agent, s.role, s.chat_id, s.thread_ts, t.id, t.origin, t.started_at FROM turns t "
        "JOIN sessions s ON s.id = t.session_id WHERE t.status = 'running' ORDER BY t.started_at, t.id").fetchall())
    keys = ("session_id", "agent", "role", "chat_id", "thread_ts", "turn_id", "origin", "started_at")
    return {"turns": [dict(zip(keys, row)) for row in rows]}


async def find(engine: Engine, session_id: str) -> Session | None:
    """A storm session by its id or by its CLI's native id (the id
    observation lists it under); the id wins, then the newest."""
    row = await engine.store.read(lambda conn: conn.execute(
        "SELECT id, agent, role, json_extract(spec, '$.cwd'), spec, paused FROM sessions WHERE id = :sid OR "
        "(native_id = :sid AND retired_at IS NULL) ORDER BY id = :sid DESC, seq DESC LIMIT 1",
        {"sid": session_id}).fetchone())
    if row is None:
        return None
    session_id, agent, role, cwd, spec_json, paused = row
    spec = spec_from_json(spec_json)
    return Session(id=session_id, agent=agent, role=role, cwd=cwd, comms=_has_comms(spec), model=spec.model,
                   effort=spec.effort, permission_mode=spec.permissions.mode, paused=paused)


# ── a new session ────────────────────────────────────────────────────────────

#: One plain session is created at a time per engine, so a retry that
#: overlaps its original finds the first one's wake instead of making a
#: second session. Only the store's checks and the write run under it.
_creating: "weakref.WeakKeyDictionary[Engine, asyncio.Lock]" = weakref.WeakKeyDictionary()


async def _start(engine: Engine, to: Route, req: Prompt, wake: Wake) -> tuple[str, str | None]:
    agent = to.agent
    runtime = engine.runtime(agent)
    adopted = to.adopted
    if adopted is not None and await asyncio.to_thread(sessions_io.find_session_file, adopted.chat_id) is None:
        raise PlainRefused("transcript_gone", f"Chat {adopted.chat_id}'s transcript is gone, so it can't be "
                                              "continued; start a new chat.", status=409)
    cwd = await asyncio.to_thread(_folder, adopted.cwd if adopted else req.cwd)
    await _logged_in(agent, runtime)
    model, effort = await _intelligence(runtime, req.model, req.effort)
    result = presets.build(PresetContext(preset=PRESET, agent=agent, cwd=cwd, intelligence=Intelligence(model, effort),
                                         comms=server_for(ROLE) if req.comms else None,
                                         request={"permission_mode": req.permission_mode, "comms": bool(req.comms)}),
                           runtime.features())

    def first_input(w: Writes, session_id: str) -> None:
        if adopted is not None:             # its process resumes the chat's own transcript
            w.execute("UPDATE sessions SET native_id = ? WHERE id = ?", (adopted.native_id, session_id))
        w.wake(session_id, wake)
    async with _creating.setdefault(engine, asyncio.Lock()):
        if (retried := await _delivered(engine, wake.id)) is not None:
            return retried
        if adopted is not None and (resumed := await find(engine, adopted.native_id)) is not None:
            await engine.deliver(resumed.id, wake, confirm_stop_runs=req.confirm_stop_runs)
            return await _delivered(engine, wake.id)
        session_id = await engine.create_session(result.spec, agent=agent, role=ROLE, preset=PRESET,
                                                 requires=result.requires, also=first_input)
    return session_id, None


async def _logged_in(agent: str, runtime: c.Runtime) -> None:
    try:
        info = await runtime.describe()
    except c.RuntimeFailure as exc:
        raise PlainRefused(exc.code, f"{agent} can't start a chat now: {exc.message}", status=409) from exc
    if not info.logged_in:
        raise PlainRefused("auth_required", f"{agent} isn't logged in. Connect it, then send again.", status=409)


def _folder(cwd: str | None) -> str:
    """The session's folder: the request's, inside the projects root (so
    observation gets its session-index row), else the root itself. Blocking."""
    root = Path(os.path.realpath(xo_projects_root()))
    if cwd is None:
        return str(root)
    asked = Path(cwd).expanduser()
    real = Path(os.path.realpath(asked if asked.is_absolute() else root / asked))
    if real != root and root not in real.parents:
        raise PlainRefused("outside_projects", f"{cwd} isn't inside the projects folder {root}; choose a folder "
                                               "there, or leave out cwd for the projects folder itself.")
    if not real.is_dir():
        raise PlainRefused("no_such_folder", f"There is no folder {real}; create it first, or choose another.")
    return str(real)


async def _intelligence(runtime: c.Runtime, model: str | None, effort: str | None) -> tuple[str | None, str | None]:
    """The model rule (ch 10 §4.1): only one of the runtime's own model ids
    is honoured; anything else (a profile id such as ``<agent>/main``) is
    ignored. An effort must be one the runtime offers for that model."""
    if model is None and effort is None:
        return None, None
    try:
        models = {m.id: m for m in await runtime.models()}
    except c.RuntimeFailure as exc:
        raise PlainRefused("runtime_unavailable", f"{runtime.name}'s models can't be read now ({exc.message}); try "
                                                  "again shortly.", status=503) from exc
    model = model if model in models else None
    if effort is not None:
        chosen = models.get(model or "")
        efforts = chosen.efforts if chosen and chosen.efforts else \
            runtime.features().features.get("effort", c.FeatureSupport()).values
        if effort not in efforts:
            raise PlainRefused("unsupported_effort", f"Effort {effort} isn't offered here: "
                                                     f"{', '.join(efforts) or 'none'}.")
    return model, effort


# ── an existing session ──────────────────────────────────────────────────────

async def _settings_fixed(session: Session, req: Prompt) -> None:
    """A session's folder and comms are set when it is created."""
    if req.cwd is not None and await asyncio.to_thread(_folder, req.cwd) != os.path.realpath(session.cwd):
        raise PlainRefused("fixed_setting", f"Session {session.id} works in {session.cwd}; its folder can't change. "
                                            "Leave out cwd, or start a new chat there.")
    if req.comms is not None and req.comms != session.comms:
        raise PlainRefused("fixed_setting", f"Session {session.id} was created {'with' if session.comms else 'without'}"
                                            " comms tools; that can't change. Leave out comms, or start a new chat.")


async def _update(engine: Engine, session: Session, req: Prompt) -> None:
    """A model, effort or mode that differs from the session's applies from
    its next turn, where the runtime allows; the engine checks the effort
    and mode, and the model rule applies as for a new session."""
    model = None
    if req.model is not None and req.model != session.model:
        model, _effort = await _intelligence(await engine.session_runtime(session.id), req.model, None)
    changes = {k: v for k, v in (("model", model), ("effort", req.effort), ("permission_mode", req.permission_mode))
               if v is not None and v != getattr(session, k)}
    if changes:
        await engine.update(session.id, **changes)


# ── reads ────────────────────────────────────────────────────────────────────

async def _delivered(engine: Engine, wake_id: str) -> tuple[str, str | None] | None:
    """The session a wake went to and the turn that took it, if it exists."""
    row = await engine.store.read(lambda conn: conn.execute("SELECT session_id, turn_id FROM wakes WHERE id = ?",
                                                            (wake_id,)).fetchone())
    return (row[0], row[1]) if row else None


def _same_target(delivered: tuple[str, str | None], to: Route) -> tuple[str, str | None]:
    """A retried ``client_id`` answers its first delivery; one reused for
    another session is refused."""
    session_id = to.session.id if to.session is not None else None
    if session_id is not None and delivered[0] != session_id:
        raise PlainRefused("client_id_reused", "This client_id was already used for another message; send a new "
                                               "one for each message.", status=409)
    return delivered


def _answer(session_id: str, turn_id: str | None) -> dict[str, Any]:
    return {"session_id": session_id, "turn_id": turn_id}


def _index_row(chat_id: str) -> Mapping[str, Any] | None:
    """The session-index row XO wrote for a chat, by the id it gave the
    client. Blocking."""
    for _project, _folder_path, index in sessions_io.iter_project_session_indexes():
        for row in index.values():
            if row.get("sessionId") == chat_id:
                return row
    return None


def _has_comms(spec: c.SessionSpec) -> bool:
    return any(s.name == COMMS_SERVER for s in spec.mcp_servers)


def _has_runtime(engine: Engine | None, agent: str) -> bool:
    if engine is not None:
        return engine.runtime(agent) is not None
    return agent in storm_engine.runtime_providers()


def _offers_no_chat(agent: str) -> bool:
    """An adapter with no chat at all: no prompt handler of its own and no
    ``stream`` for the dispatcher. Read from its class, never built; an agent
    with no adapter (or no such agent) keeps today's path as it is."""
    try:
        chat = try_load_capability("chat", agent=agent)
        module = try_load_capability("adapter", agent=agent)
    except ValueError:                      # not an agent name: today's path answers it
        return False
    adapter = getattr(module, "Adapter", None) if module is not None else None
    if adapter is None or getattr(chat, "handle_prompt", None) is not None:
        return False
    return not adapter.chats()


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
