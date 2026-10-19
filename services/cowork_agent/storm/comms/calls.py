"""Who is calling, and running one tool call (design ch 12 §1, §2.2, §7).

**The token decides everything.** A comms token is bound to (session,
epoch) and only its sha-256 is stored (``comms_tokens``). An unknown token
is 401; a token of an epoch the session has moved on from is 409
``epoch_stale``, so a stale process can't act; a retired session is 401.
The caller's role, chat and thread come from its session row, never from
the call.

**Every call is one store transaction** with its rows, hop and wake, and a
``comms.call`` event (``{tool, kind, ok, …refs}``, never a message body, a
token or memory content): the reply gate and the routing record are built
from those events. A refused call is recorded too (``ok: false`` with its
code), in a transaction of its own. Calls return at once: no tool waits for
the person or another agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from services.cowork_agent.storm.comms import tools
from services.cowork_agent.storm.engine import Engine, Writes
from services.cowork_agent.storm.engine.specs import token_digest
from services.errors import ServiceError

log = logging.getLogger(__name__)

#: The result fields a ``comms.call`` event keeps, so a call can be traced to
#: what it made.
REFS = ("message_id", "thread_id", "hop_id", "review_id", "session_id", "todo_id")
UNEXPECTED = "The server could not run that tool."
STALE = "This process belongs to an earlier start of its session; a newer one runs it now."


class CommsError(ServiceError):
    """A refused call: its one plain sentence goes back to the agent as an
    MCP tool error (ch 12 §7)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, status=400)


class TokenRefused(ServiceError):
    """The caller isn't a live session's current process."""


@dataclass(frozen=True)
class Caller:
    session_id: str
    role: str
    epoch: int
    agent: str
    chat_id: str | None
    thread_ts: str | None
    cwd: str


async def resolve(engine: Engine, token: str) -> Caller:
    row = await engine.store.read(lambda conn: conn.execute(
        "SELECT t.epoch, s.epoch, s.retired_at, s.id, s.role, s.agent, s.chat_id, s.thread_ts, "
        "json_extract(s.spec, '$.cwd') FROM comms_tokens t JOIN sessions s ON s.id = t.session_id "
        "WHERE t.sha256 = ?", (token_digest(token),)).fetchone())
    if row is None:
        raise TokenRefused("unknown_token", "Unknown comms token.", status=401)
    token_epoch, epoch, retired_at, *who = row
    if retired_at is not None:
        raise TokenRefused("session_retired", "This session was retired; its successor has its own token.",
                           status=401)
    if token_epoch != epoch:
        raise TokenRefused("epoch_stale", STALE, status=409)
    session_id, role, agent, chat_id, thread_ts, cwd = who
    return Caller(session_id=session_id, role=role, epoch=epoch, agent=agent, chat_id=chat_id, thread_ts=thread_ts,
                  cwd=cwd)


async def caller_of(engine: Engine, session_id: str) -> Caller:
    """A session as the comms tools see it, for a call XO makes on its
    behalf (a decision model routing for a coordinator, ch 11 §3.1)."""
    row = await engine.store.read(lambda conn: conn.execute(
        "SELECT id, role, epoch, agent, chat_id, thread_ts, json_extract(spec, '$.cwd') FROM sessions WHERE id = ?",
        (session_id,)).fetchone())
    session_id, role, epoch, agent, chat_id, thread_ts, cwd = row
    return Caller(session_id=session_id, role=role, epoch=epoch, agent=agent, chat_id=chat_id, thread_ts=thread_ts,
                  cwd=cwd)


class ToolCall:
    """One call as a handler sees it. A handler does its async work first,
    then writes exactly once: ``write(fn)``, or ``record(w, result)`` inside
    a transaction it runs itself (``create_session``'s ``also``)."""

    def __init__(self, chats: Any, caller: Caller, name: str, args: Mapping[str, Any]) -> None:
        self.chats = chats
        self.caller = caller
        self.name = name
        self.args = args
        self.recorded = False

    @property
    def engine(self) -> Engine:
        return self.chats.engine

    async def still_current(self) -> None:
        """Before a file write (memory, todos), which no transaction can take
        back: refuse if a newer process has attached since the call came in."""
        epoch = await self.engine.store.read(lambda conn: conn.execute(
            "SELECT epoch FROM sessions WHERE id = ?", (self.caller.session_id,)).fetchone()[0])
        if epoch != self.caller.epoch:
            raise TokenRefused("epoch_stale", STALE, status=409)

    async def write(self, fn: Callable[[Writes], dict[str, Any]]) -> dict[str, Any]:
        return await self.engine.write(lambda w: self.record(w, fn(w)))

    def record(self, w: Writes, result: dict[str, Any], **data: Any) -> dict[str, Any]:
        """Append the call's ``comms.call`` event in ``w``; returns ``result``."""
        if self.recorded:
            raise RuntimeError(f"{self.name} recorded twice")
        self.recorded = True
        refs = {k: result[k] for k in REFS if result.get(k) is not None}
        _append(w, self.caller, self.name, ok=True, **refs, **data)
        return result


Handler = Callable[[ToolCall], Awaitable[dict[str, Any]]]


async def call(chats: Any, caller: Caller, name: Any, args: Any, handlers: Mapping[str, Handler]) -> dict[str, Any]:
    """Run one tool for ``caller``. Raises ``CommsError`` with the sentence
    the agent sees; an unexpected failure becomes ``UNEXPECTED`` for the
    agent, with the trace in the log only."""
    try:
        if not isinstance(name, str) or name not in tools.ROLE_TOOLS[caller.role]:
            raise CommsError("unknown_tool", f"{name!r} is not a tool for a {caller.role}.")
        if not isinstance(args, Mapping):
            raise CommsError("invalid_arguments", "Arguments must be an object.")
        ctx = ToolCall(chats, caller, name, args)
        result = await handlers[name](ctx)
        if not ctx.recorded:
            raise RuntimeError(f"{name} returned without recording its call")
        return result
    except TokenRefused:
        raise                               # its process moved on meanwhile: the endpoint answers 409
    except ServiceError as exc:
        await _refused(chats.engine, caller, name, exc.code)
        raise CommsError(exc.code, exc.message) from exc
    except Exception as exc:
        log.exception("xo_comms: %s failed for session %s", name, caller.session_id)
        await _refused(chats.engine, caller, name, "server_error")
        raise CommsError("server_error", UNEXPECTED) from exc


async def _refused(engine: Engine, caller: Caller, name: Any, code: str) -> None:
    tool = name if isinstance(name, str) and name in tools.TOOLS else None
    try:
        await engine.write(lambda w: _append(w, caller, tool, ok=False, error=code))
    except Exception:  # noqa: BLE001 - the agent still gets its answer; the log has why the record failed
        log.exception("xo_comms: could not record a refused %s call for session %s", tool, caller.session_id)


def _append(w: Writes, caller: Caller, tool: str | None, *, ok: bool, **data: Any) -> None:
    if ok and w.execute("SELECT epoch FROM sessions WHERE id = ?", (caller.session_id,)).fetchone()[0] != caller.epoch:
        # A new process attached while the call ran: like every engine write,
        # the call is compare-and-set on its epoch (ch 07 §9.2).
        raise TokenRefused("epoch_stale", STALE, status=409)
    w.append("comms.call", {"tool": tool, "kind": tools.kind(tool) if tool else None, "role": caller.role, "ok": ok,
                            **data}, chat_id=caller.chat_id, session_id=caller.session_id, thread_ts=caller.thread_ts,
             turn_id=current_turn(w, caller.session_id), epoch=caller.epoch)


def current_turn(w: Writes, session_id: str) -> str | None:
    """The session's running turn, which a call's events belong to."""
    row = w.execute("SELECT id FROM turns WHERE session_id = ? AND status = 'running' ORDER BY started_at DESC "
                    "LIMIT 1", (session_id,)).fetchone()
    return row[0] if row else None
