"""Chats and threads: the conversation layer on the engine and the store
(design ch 11). Workspace and project chats, their timelines, the sessions
that speak for them (one live coordinator per chat), threads and their
sessions, hops, chat memory.

The comms tools (``storm/comms/``) are how agents act on it; the routes
(``routers/cowork_agent/storm/``) are how the person does. Every change is one
engine transaction (``engine.write``, or ``create_session``'s ``also`` when it
starts a session), with its events. A session's agent is chosen once, at
creation, and kept for life (ch 10 §3): the conversation's pin, else the
active agent; it must have a storm runtime and be logged in, or creation
fails plainly.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from services.cowork_agent import project_layout
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import presets
from services.cowork_agent.storm.chats import rows
from services.cowork_agent.storm.comms.tools import server_for
from services.cowork_agent.storm.engine import Engine, EngineUnavailable, Writes
from services.cowork_agent.storm.presets import Intelligence, PresetContext
from services.cowork_agent.storm.worktrees import place
from services.errors import ServiceError
from services.xo_manifest import resolve_agent_name

T = TypeVar("T")

#: The coordinator role and preset of each chat kind.
COORDINATOR = {rows.WORKSPACE: ("workspace-coordinator", "workspace_coordinator"),
               rows.PROJECT: ("project-coordinator", "project_coordinator")}

_PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
#: How much of a project's description the coordinators are shown.
DESCRIPTION_MAX = 300
README_LINES = 12

_chats: "Chats | None" = None


class ChatRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(code, message, status=status)


def live_coordinator(conn, chat_id: str) -> str | None:
    """The chat's live coordinator session, if it has one (store read)."""
    row = conn.execute("SELECT id FROM sessions WHERE chat_id = ? AND role LIKE '%-coordinator' AND retired_at IS NULL "
                       "ORDER BY seq DESC LIMIT 1", (chat_id,)).fetchone()
    return row[0] if row else None


def project_folders() -> list[str]:
    """Every direct, non-hidden sub-folder of the projects root: each is a
    project with a chat (ch 11 §1). Blocking."""
    root = project_layout.xo_projects_root()
    try:
        return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []


def valid_project_name(name: str) -> bool:
    """A new project's folder name: one safe segment (letters, digits, ``-``
    and ``_``), as a runtime key must be."""
    return bool(_PROJECT_NAME.fullmatch(name))


def project_pid(name: str) -> str | None:
    """The project's pid from its ``.xo/project.json``, once it has one. Blocking."""
    meta = project_layout.load_project(name) or {}
    pid = meta.get("pid")
    return str(pid) if pid and not meta.get("_template") else None


def describe_project(name: str) -> str:
    """One line about a project: its ``project.json`` description, else the
    start of its README. Blocking."""
    meta = project_layout.load_project(name) or {}
    if meta.get("description"):
        return str(meta["description"])[:DESCRIPTION_MAX]
    for readme in ("README.md", "README", "PROJECT.md"):
        try:
            lines = (project_layout.project_dir(name) / readme).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = " ".join(line.strip("# ").strip() for line in lines.splitlines()[:README_LINES] if line.strip())
        if text:
            return text[:DESCRIPTION_MAX]
    return ""


async def check_intelligence(runtime: c.Runtime, intelligence: Intelligence) -> None:
    """A pinned model must be one of the runtime's, and an effort one it
    offers for that model (ch 10 §3.3)."""
    if intelligence.model is None and intelligence.effort is None:
        return
    try:
        models = {m.id: m for m in await runtime.models()}
    except c.RuntimeFailure as exc:
        raise ChatRefused("runtime_unavailable", f"{runtime.name}'s models can't be read now ({exc.message}); try "
                                                 "again shortly.", status=503) from exc
    if intelligence.model is not None and intelligence.model not in models:
        raise ChatRefused("unsupported_model", f"{intelligence.model} isn't one of {runtime.name}'s models: "
                                               f"{', '.join(sorted(models))}.")
    if intelligence.effort is not None:
        chosen = models.get(intelligence.model or "")
        efforts = chosen.efforts if chosen and chosen.efforts else \
            runtime.features().features.get("effort", c.FeatureSupport()).values
        if intelligence.effort not in efforts:
            raise ChatRefused("unsupported_effort", f"Effort {intelligence.effort} isn't offered here: "
                                                    f"{', '.join(efforts) or 'none'}.")


class Chats:
    def __init__(self, engine: Engine, queue: Any) -> None:
        self.engine = engine
        self.queue = queue                      # the review queue: asks open items through it
        self._locks: dict[str, asyncio.Lock] = {}
        self._memory_locks: dict[str, asyncio.Lock] = {}

    # ── chats ────────────────────────────────────────────────────────────

    async def chat(self, chat_id: str) -> dict[str, Any]:
        found = await self.engine.store.read(lambda conn: rows.get_chat(conn, chat_id))
        if found is None:
            raise ChatRefused("chat_not_found", f"There is no chat {chat_id!r}. List them with GET /api/chat/chats.",
                              status=404)
        return found

    async def workspace(self) -> dict[str, Any]:
        found = await self.engine.store.read(
            lambda conn: conn.execute(f"SELECT {rows.CHAT_COLUMNS} FROM chats WHERE kind = 'workspace'").fetchone())
        return rows.chat_view(found) if found else await self.engine.write(rows.workspace_chat)

    async def project(self, name: str) -> dict[str, Any]:
        """The chat of the project folder ``name``, created the first time."""
        if name not in await asyncio.to_thread(project_folders):
            raise ChatRefused("no_such_project", f"There is no project {name!r} in the workspace.", status=404)
        pid = await asyncio.to_thread(project_pid, name)
        known = await self.engine.store.read(lambda conn: conn.execute(
            f"SELECT {rows.CHAT_COLUMNS} FROM chats WHERE kind = 'project' AND project_pid = ? AND project_name = ?",
            (pid or name, name)).fetchone())
        if known is not None:
            return rows.chat_view(known)
        chat, _created = await self.engine.write(lambda w: rows.project_chat(w, name=name, pid=pid))
        return chat

    @staticmethod
    def folder(chat: Mapping[str, Any]) -> Path:
        """Where the chat's coordinator works: the projects root, or the project folder."""
        root = Path(os.path.realpath(project_layout.xo_projects_root()))
        return root if chat["kind"] == rows.WORKSPACE else root / chat["project"]

    # ── the sessions that speak for a chat ───────────────────────────────

    async def with_coordinator(self, chat: Mapping[str, Any], fn: Callable[[Writes, str], T]) -> T:
        """Run ``fn(w, coordinator session)`` in one transaction, starting the
        chat's coordinator (incarnation 1) in that same transaction when it
        has none (ch 11 §6.1). One chat's coordinators start one at a time."""
        role, preset = COORDINATOR[chat["kind"]]
        async with self._lock(chat["id"]):
            live = await self.engine.store.read(lambda conn: live_coordinator(conn, chat["id"]))
            if live is not None:
                return await self.engine.write(lambda w: fn(w, live))
            pin = await self._pin("chat", chat["id"])
            agent, runtime = await self._runtime(pin.get("agent"), f"run this chat's {role}")
            result = presets.build(PresetContext(preset=preset, agent=agent, cwd=str(self.folder(chat)),
                                                 project=chat.get("project"),
                                                 intelligence=Intelligence(pin.get("model"), pin.get("effort")),
                                                 comms=server_for(role)), runtime.features())
            out: list[T] = []
            await self.engine.create_session(result.spec, agent=agent, role=role, preset=preset, chat_id=chat["id"],
                                             requires=result.requires, also=lambda w, sid: out.append(fn(w, sid)))
            return out[0]

    async def start_thread(self, chat: Mapping[str, Any], ts: str, title: str, intelligence: Intelligence,
                           also: Callable[[Writes, str], T]) -> T:
        """A thread rooted on the timeline message ``ts`` and its session, in
        one transaction with ``also(w, thread session)`` (ch 11 §7.1). Its
        folder is its worktree, or the project folder when the project isn't
        a git repository, said on the thread (ch 13 §2.4). A model or effort
        chosen for it is checked against its agent's catalog and kept as the
        thread's own until the person pins another (ch 11 §3.3)."""
        where = await asyncio.to_thread(place, self.folder(chat), ts)
        agent, runtime = await self._runtime(None, "run a thread")
        await check_intelligence(runtime, intelligence)
        result = presets.build(PresetContext(preset="thread", agent=agent, cwd=where.cwd, project=chat["project"],
                                             intelligence=intelligence, comms=server_for("thread")),
                               runtime.features())
        out: list[T] = []

        def create(w: Writes, session_id: str) -> None:
            if rows.get_thread(w, chat["id"], ts) is not None:
                raise ChatRefused("thread_exists", "That message already has a thread; send it a follow-up "
                                                   "instead.", status=409)
            rows.add_thread(w, chat_id=chat["id"], ts=ts, title=title, session_id=session_id, cwd=where.cwd)
            if where.note:
                rows.add_message(w, chat_id=chat["id"], thread_ts=ts, author_kind="system", author_id="xo",
                                 text=where.note)
            if intelligence.model or intelligence.effort:
                w.execute("INSERT INTO intelligence (scope, scope_id, agent, model, effort, set_by, set_at) "
                          "VALUES ('thread', ?, ?, ?, ?, 'router', ?)",
                          (thread_scope(chat["id"], ts), agent, intelligence.model, intelligence.effort, w.now))
            out.append(also(w, session_id))
        await self.engine.create_session(result.spec, agent=agent, role="thread", preset="thread",
                                         chat_id=chat["id"], thread_ts=ts, requires=result.requires, also=create)
        return out[0]

    async def _pin(self, scope: str, scope_id: str) -> dict[str, Any]:
        row = await self.engine.store.read(lambda conn: conn.execute(
            "SELECT agent, model, effort FROM intelligence WHERE scope = ? AND scope_id = ?",
            (scope, scope_id)).fetchone())
        return dict(zip(("agent", "model", "effort"), row)) if row else {}

    async def _runtime(self, pinned: str | None, purpose: str) -> tuple[str, c.Runtime]:
        """The agent a new session runs on (ch 10 §3.2) and its runtime; it
        must have one and be logged in."""
        agent = pinned or resolve_agent_name()
        runtime = self.engine.runtime(agent)
        if runtime is None:
            raise ChatRefused("no_runtime", f"{agent} can't {purpose}: it has no storm runtime. Pin an agent that "
                                            "has one.", status=409)
        try:
            info = await runtime.describe()
        except c.RuntimeFailure as exc:
            raise ChatRefused(exc.code, f"{agent} can't {purpose} now: {exc.message}", status=409) from exc
        if not info.logged_in:
            raise ChatRefused("auth_required", f"{agent} can't {purpose}: it isn't logged in. Connect it, then try "
                                               "again.", status=409)
        return agent, runtime

    def _lock(self, chat_id: str) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    def memory_lock(self, chat_id: str) -> asyncio.Lock:
        """One memory write per chat at a time: the file and its stored
        version move together."""
        return self._memory_locks.setdefault(chat_id, asyncio.Lock())


def thread_scope(chat_id: str, ts: str) -> str:
    """A thread's id among the ``intelligence`` scopes."""
    return f"{chat_id}:{ts}"


def boot(engine: Engine, queue: Any) -> Chats:
    global _chats
    _chats = Chats(engine, queue)
    return _chats


def shutdown() -> None:
    global _chats
    _chats = None


def current() -> Chats:
    if _chats is None:
        raise EngineUnavailable("engine_not_running", "Storm's chats aren't running in this server (they start with "
                                                      "the engine); retry shortly.")
    return _chats
