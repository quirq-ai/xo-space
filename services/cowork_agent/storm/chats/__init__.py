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
import logging
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, TypeVar

from services import project_management
from services.cowork_agent import project_layout
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import presets
from services.cowork_agent.storm.chats import memory, recycle, rows
from services.cowork_agent.storm.comms.tools import server_for
from services.cowork_agent.storm.engine import Engine, EngineUnavailable, Writes
from services.cowork_agent.storm.engine.catalog import Catalog
from services.cowork_agent.storm.engine.specs import spec_from_json
from services.cowork_agent.storm.presets import Intelligence, PresetContext
from services.cowork_agent.storm.worktrees import place
from services.errors import ServiceError
from services.xo_manifest import resolve_agent_name

T = TypeVar("T")
log = logging.getLogger(__name__)

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


def pid_keys(pids: Mapping[str, str | None], holders: Mapping[str, str]) -> dict[str, str | None]:
    """The pid each project folder's chat is keyed by. A pid two folders
    hold (a copied project keeps its ``.xo/project.json``) stays with the
    folder whose chat already has it, else the first by name; the others are
    keyed by their folder name, so a copy never shares its original's chat."""
    keys: dict[str, str | None] = {}
    for name, pid in pids.items():
        sharing = sorted(n for n, p in pids.items() if p is not None and p == pid)
        keeper = holders.get(pid) if holders.get(pid) in sharing else (sharing[0] if sharing else None)
        keys[name] = pid if pid is not None and name == keeper else None
    return keys


async def read_catalog(runtime: c.Runtime) -> Catalog:
    """The runtime's catalog; ``ChatRefused`` (503) when its models can't be
    read now."""
    try:
        models = await runtime.models()
    except c.RuntimeFailure as exc:
        raise ChatRefused("runtime_unavailable", f"{runtime.name}'s models can't be read now ({exc.message})",
                          status=503) from exc
    return Catalog.of(runtime.name, models, runtime.features())


async def check_intelligence(runtime: c.Runtime, intelligence: Intelligence) -> None:
    """A pinned model must be one of the runtime's, and an effort one it
    offers for that model (ch 10 §3.3)."""
    if intelligence.model is None and intelligence.effort is None:
        return
    try:
        catalog = await read_catalog(runtime)
    except ChatRefused as exc:
        raise ChatRefused(exc.code, f"{exc.message}; try again shortly.", status=exc.status) from exc
    problem = catalog.problem(intelligence.model, intelligence.effort)
    if problem == "unsupported_model":
        raise ChatRefused(problem, f"{intelligence.model} isn't one of {runtime.name}'s models: "
                                   f"{', '.join(sorted(m.id for m in catalog.models))}.")
    if problem == "unsupported_effort":
        raise ChatRefused(problem, f"Effort {intelligence.effort} isn't offered here: "
                                   f"{', '.join(catalog.efforts(intelligence.model)) or 'none'}.")


#: How a thread that runs on its preset's default model and effort says so,
#: on the thread and to whoever chose (§0.105).
ON_DEFAULT = "so this thread runs on the default model and effort"


async def routed_intelligence(runtime: c.Runtime, chosen: Intelligence, *, must_choose: bool,
                              unchosen: str | None) -> tuple[Intelligence, str | None]:
    """The router's choice of a new thread's model and effort, as the
    thread will run (§0.105, ch 11 §3.3): taken whole when the agent's
    catalog has it, as a pin is; otherwise none of it is used and the thread
    runs on its preset's default. The second value is then the one sentence
    saying why, with the catalog when the choice wasn't in it.

    An agent ``must_choose``: a model, and an effort for a model that takes
    one, or the call is refused with the catalog (``missing_argument``);
    with the catalog unreadable nothing could be chosen, so the thread runs
    on the default. ``unchosen`` is why a caller that may leave the choice
    out (an unsure decision model) did. A catalog that lists no models is
    one nothing can be chosen from. The catalog's default model means no
    model override (the CLI's own); a model that takes no effort runs with
    none."""
    nothing = chosen.model is None and chosen.effort is None
    if nothing and not must_choose:
        return chosen, f"{unchosen}, {ON_DEFAULT}." if unchosen else None
    try:
        catalog = await read_catalog(runtime)
    except ChatRefused as exc:
        return Intelligence(), f"{exc.message}, {ON_DEFAULT}."
    if not catalog.models:
        return Intelligence(), f"{catalog.agent} lists no models now, {ON_DEFAULT}."
    if must_choose and chosen.model is None:
        raise ChatRefused("missing_argument", f"Choose the thread's model: {catalog.agent} offers "
                                              f"{catalog.describe()}.")
    problem = catalog.problem(chosen.model, chosen.effort)
    if problem is not None:
        wrong = (f"The model {chosen.model} isn't one of {catalog.agent}'s" if problem == "unsupported_model" else
                 f"Effort {chosen.effort} isn't offered with {chosen.model or 'the default model'}")
        return Intelligence(), f"{wrong}, {ON_DEFAULT}; {catalog.agent} offers {catalog.describe()}."
    if must_choose and chosen.effort is None and catalog.efforts(chosen.model):
        raise ChatRefused("missing_argument", f"Choose the thread's effort: {chosen.model} takes "
                                              f"{', '.join(catalog.efforts(chosen.model))}.")
    model = catalog.model(chosen.model)
    if model is None:
        return chosen, None
    return Intelligence(None if model.is_default else model.id, chosen.effort if model.takes_effort else None,
                        takes_effort=model.takes_effort), None


#: How long a coordinator's tool listing, or a decision model's routing,
#: waits for the thread agent's catalog. A cold models probe can take a
#: minute, and an agent CLI waits only about 30 s for an MCP server to start:
#: a coordinator must never start without its comms. The read runs on.
CATALOG_WAIT_S = 5.0
#: What a thread's agent is asked to do, in the sentence that refuses one.
THREAD_PURPOSE = "run a thread"

#: Who stops a removed project's running turns.
REMOVAL = "xo"


class Chats:
    def __init__(self, engine: Engine, queue: Any) -> None:
        self.engine = engine
        self.queue = queue                      # the review queue: asks open items through it
        self._locks: dict[str, asyncio.Lock] = {}
        self._memory_locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._stopped = False
        self._catalog_read: asyncio.Task | None = None      # the bounded readers' one shared read

    # ── its own work ─────────────────────────────────────────────────────

    def spawn(self, work: Awaitable[Any], name: str) -> asyncio.Task | None:
        """A task the conversation owns (an input's follow-up, a decision
        model's routing): logged if it fails, cancelled at shutdown; none
        starts after it (None)."""
        if self._stopped:
            if asyncio.iscoroutine(work):
                work.close()
            return None
        task = asyncio.ensure_future(work)
        task.set_name(f"storm-chats:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("storm chats: %s failed", task.get_name(), exc_info=task.exception())

    async def stop(self) -> None:
        self._stopped = True
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

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
        pid = (await self.pid_keys())[name]
        known = await self.engine.store.read(lambda conn: conn.execute(
            f"SELECT {rows.CHAT_COLUMNS} FROM chats WHERE kind = 'project' AND project_pid = ? AND project_name = ?",
            (pid or name, name)).fetchone())
        if known is not None:
            return rows.chat_view(known)
        chat, _created = await self.engine.write(lambda w: rows.project_chat(w, name=name, pid=pid))
        return chat

    async def pid_keys(self) -> dict[str, str | None]:
        """Every project folder's chat key (``pid_keys``)."""
        pids = await asyncio.to_thread(lambda: {n: project_pid(n) for n in project_folders()})
        holders = await self.engine.store.read(lambda conn: dict(conn.execute(
            "SELECT project_pid, project_name FROM chats WHERE kind = 'project'").fetchall()))
        return pid_keys(pids, holders)

    @staticmethod
    def writable(chat: Mapping[str, Any]) -> None:
        """A retired chat (its project removed through XO) is read-only."""
        if chat.get("retired_at"):
            raise ChatRefused("chat_retired", "This chat's project was removed, so the chat is kept read-only. Start "
                                              "the work in a current project's chat.", status=409)

    async def retire_project(self, name: str, pid: str | None) -> str | None:
        """The project ``name`` was removed through XO (§0.102): retire its
        chat, the chat's live sessions and their open review items in one
        transaction, then stop their running turns and release their
        processes. Its pid finds its chat only while no remaining folder
        holds that pid (a removed copy must not retire its original's).
        Returns the chat's id, or None when it had none."""
        if pid is not None and pid in await asyncio.to_thread(lambda: {project_pid(n) for n in project_folders()}):
            pid = None

        def retire(w: Writes) -> tuple[str, list[str]] | None:
            retired = rows.retire_project_chat(w, name=name, pid=pid)
            if retired is not None and self.queue is not None:
                marks = ", ".join("?" * len(retired[1]))
                for (item,) in w.execute(f"SELECT id FROM review_items WHERE state = 'open' AND session_id IN "
                                         f"({marks})", retired[1]).fetchall():
                    self.queue.withdraw(w, item, "withdrawn")
            return retired
        retired = await self.engine.write(retire)
        if retired is None:
            return None
        chat_id, sessions = retired
        # Nothing more runs in a removed project: the stops go out together, and one that fails
        # still leaves every process released.
        stops = await asyncio.gather(*(self.engine.stop(sid, by=REMOVAL) for sid in sessions), return_exceptions=True)
        for session_id, stop in zip(sessions, stops):
            if isinstance(stop, BaseException):
                log.warning("storm chats: stopping session %s of removed project %s failed: %s", session_id, name,
                            stop)
            self.engine.retired(session_id)
        log.info("storm chats: chat %s retired with its removed project %s", chat_id, name)
        return chat_id

    @staticmethod
    def folder(chat: Mapping[str, Any]) -> Path:
        """Where the chat's coordinator works: the projects root, or the project folder."""
        root = Path(os.path.realpath(project_layout.xo_projects_root()))
        return root if chat["kind"] == rows.WORKSPACE else root / chat["project"]

    # ── the sessions that speak for a chat ───────────────────────────────

    async def with_coordinator(self, chat: Mapping[str, Any], fn: Callable[[Writes, str], T]) -> T:
        """Run ``fn(w, coordinator session)`` in one transaction, starting the
        chat's coordinator in that same transaction when it has none
        (incarnation 1, ch 11 §6.1) or when the live one is due to be
        recycled and between inputs (its successor, §6.2). When the successor
        can't start or the live one took an input meanwhile, the live one
        gets it. One chat's coordinators start one at a time."""
        async with self._lock(chat["id"]):
            # Read now: a chat retired since the caller read it starts nothing (§0.102).
            self.writable(await self.chat(chat["id"]))
            live = await self.engine.store.read(lambda conn: live_coordinator(conn, chat["id"]))
            if live is None:
                return await self._start_coordinator(chat, fn, predecessor=None, why=None)
            due = await self._due(chat, live)
            if due is not None:
                done = await self._recycle(chat, fn, live, due)
                if done:
                    return done[0]
            return await self.engine.write(lambda w: fn(w, live))

    async def recycle_if_due(self, chat: Mapping[str, Any]) -> str | None:
        """Retire the chat's coordinator now if it is due and idle; returns why."""
        async with self._lock(chat["id"]):
            live = await self.engine.store.read(lambda conn: live_coordinator(conn, chat["id"]))
            due = await self._due(chat, live) if live is not None else None
            if due is not None and await self._recycle(chat, lambda w, session_id: None, live, due):
                return due
            return None

    async def _recycle(self, chat: Mapping[str, Any], fn: Callable[[Writes, str], T], live: str,
                       why: str) -> list[T]:
        """Replace ``live`` by its successor, with ``fn`` in that transaction;
        ``[fn's result]``, or ``[]`` when it stays (said in the log)."""
        try:
            built = await self._coordinator(chat)
        except ChatRefused as exc:
            log.warning("storm chats: %s's coordinator stays, its successor can't start (%s): %s", chat["id"],
                        exc.code, exc.message)
            return []
        try:
            return [await self._start_coordinator(chat, fn, predecessor=live, why=why, built=built)]
        except recycle.Busy:
            log.info("storm chats: %s's coordinator took an input; it is recycled at its next boundary", chat["id"])
            return []

    async def _coordinator(self, chat: Mapping[str, Any]) -> tuple[str, str, Any]:
        """The chat's coordinator as its preset builds it now: the agent (its
        runtime logged in), the role and the preset's result."""
        pin = await self._pin("chat", chat["id"])
        role, _preset = COORDINATOR[chat["kind"]]
        agent, runtime = await self.runtime_for(pin.get("agent"), f"run this chat's {role}")
        return agent, role, self._preset(chat, agent, runtime, pin)

    def _preset(self, chat: Mapping[str, Any], agent: str, runtime: c.Runtime, pin: Mapping[str, Any]) -> Any:
        role, preset = COORDINATOR[chat["kind"]]
        return presets.build(PresetContext(preset=preset, agent=agent, cwd=str(self.folder(chat)),
                                           project=chat.get("project"),
                                           intelligence=Intelligence(pin.get("model"), pin.get("effort")),
                                           comms=server_for(role)), runtime.features())

    async def _due(self, chat: Mapping[str, Any], session_id: str) -> str | None:
        """Why the live coordinator should be replaced now, or None; never
        while it is busy (a turn, a held call, a sub-agent), its input hasn't
        ended, or it is paused. Reads only the store and the runtime's
        feature list: nothing is probed."""
        session = await self.engine.get_session(session_id)
        if session["state"] not in recycle.BETWEEN_INPUTS or session["paused"]:
            return None             # a paused one waits for the person (the loop breaker), with its wakes
        pin = await self._pin("chat", chat["id"])
        agent_changed = bool(pin.get("agent")) and pin["agent"] != session["agent"]
        runtime = self.engine.runtime(session["agent"])
        changed = False
        if runtime is not None:
            stored = await self.engine.store.read(lambda conn: conn.execute(
                "SELECT spec FROM sessions WHERE id = ?", (session_id,)).fetchone()[0])
            fresh = self._preset(chat, session["agent"], runtime, pin)
            changed = fresh.fingerprint != presets.fingerprint(spec_from_json(stored))
        now_s = self.engine.clock.time()

        def why(conn) -> str | None:
            if not recycle.input_ended(conn, session_id):
                return None
            return recycle.reason(conn, session_id, now_s=now_s, fingerprint_changed=changed,
                                  agent_changed=agent_changed)
        return await self.engine.store.read(why)

    async def _start_coordinator(self, chat: Mapping[str, Any], fn: Callable[[Writes, str], T], *,
                                 predecessor: str | None, why: str | None,
                                 built: tuple[str, str, Any] | None = None) -> T:
        """Create the chat's coordinator with ``fn`` in its transaction; with
        a ``predecessor``, retire that one in it too, only while it is
        between inputs (else ``recycle.Busy``, nothing written)."""
        agent, role, result = built or await self._coordinator(chat)
        _role, preset = COORDINATOR[chat["kind"]]
        parts = await self.engine.store.read(lambda conn: recycle.handoff_parts(conn, chat)) if predecessor else None
        index = (await asyncio.to_thread(memory.read, chat["id"]))["content"] if predecessor else None
        out: list[T] = []

        def create(w: Writes, session_id: str) -> None:
            if predecessor is not None:
                recycle.succeed(w, old=predecessor, new=session_id, why=why, chat=chat, memory_index=index or None,
                                parts=parts)
            out.append(fn(w, session_id))

        async def make() -> None:
            await self.engine.create_session(result.spec, agent=agent, role=role, preset=preset,
                                             chat_id=chat["id"], requires=result.requires, also=create)
        if predecessor is None:
            await make()
            return out[0]
        async with self.engine.between_inputs(predecessor) as idle:
            if not idle:
                raise recycle.Busy(predecessor)
            await make()
        log.info("storm chats: coordinator %s of %s retired (%s)", predecessor, chat["id"], why)
        self.engine.retired(predecessor)
        return out[0]

    async def start_thread(self, chat: Mapping[str, Any], ts: str, title: str, chosen: Intelligence,
                           also: Callable[[Writes, str, str | None], T], *, must_choose: bool = False,
                           unchosen: str | None = None) -> T:
        """A thread rooted on the timeline message ``ts`` and its session, in
        one transaction with ``also(w, thread session, note)`` (ch 11 §7.1).
        Its folder is its worktree, or the project folder when the project
        isn't a git repository, said on the thread (ch 13 §2.4). The model
        and effort the router ``chosen`` for it are checked against its
        agent's catalog and kept as the thread's own until the person pins
        another (ch 11 §3.3); never its chat's pin (§0.103). A choice the
        catalog lacks, or one that can't be checked now, is not used: the
        thread runs on its preset's default, and ``note`` (also said on the
        thread) is why (§0.105). ``must_choose`` and ``unchosen`` are
        ``routed_intelligence``'s."""
        where = await asyncio.to_thread(place, self.folder(chat), ts)
        agent, runtime = await self.runtime_for(None, THREAD_PURPOSE)
        intelligence, note = await routed_intelligence(runtime, chosen, must_choose=must_choose, unchosen=unchosen)
        result = presets.build(PresetContext(preset="thread", agent=agent, cwd=where.cwd, project=chat["project"],
                                             intelligence=intelligence, comms=server_for("thread")),
                               runtime.features())
        out: list[T] = []

        def create(w: Writes, session_id: str) -> None:
            if rows.get_thread(w, chat["id"], ts) is not None:
                raise ChatRefused("thread_exists", "That message already has a thread; send it a follow-up "
                                                   "instead.", status=409)
            rows.add_thread(w, chat_id=chat["id"], ts=ts, title=title, session_id=session_id, cwd=where.cwd)
            for said in (where.note, note):
                if said:
                    rows.add_message(w, chat_id=chat["id"], thread_ts=ts, author_kind="system", author_id="xo",
                                     text=said)
            if intelligence.model or intelligence.effort:
                w.execute("INSERT INTO intelligence (scope, scope_id, agent, model, effort, set_by, set_at) "
                          "VALUES ('thread', ?, ?, ?, ?, 'router', ?)",
                          (thread_scope(chat["id"], ts), agent, intelligence.model, intelligence.effort, w.now))
            out.append(also(w, session_id, note))
        await self.engine.create_session(result.spec, agent=agent, role="thread", preset="thread",
                                         chat_id=chat["id"], thread_ts=ts, requires=result.requires, also=create)
        return out[0]

    async def thread_catalog(self, *, bounded: bool = False) -> Catalog:
        """The catalog a new thread's model and effort are chosen from: that
        of the agent ``start_thread`` runs it on (ch 10 §3.2). ``ChatRefused``
        when that agent has no runtime or its models can't be read now.
        ``bounded`` waits at most ``CATALOG_WAIT_S`` (then ``catalog_slow``):
        the read is one task the chats own, shared by such callers, so a
        waiter that gives up never cancels a slow models probe and the next
        caller gets its answer."""
        if not bounded:
            return await read_catalog(self._runtime(resolve_agent_name(), THREAD_PURPOSE))
        if self._catalog_read is None or self._catalog_read.done():
            self._catalog_read = self.spawn(self._catalog_outcome(), "thread-catalog")
            if self._catalog_read is None:
                raise EngineUnavailable("engine_not_running", "Storm's chats have stopped; retry shortly.")
        try:
            outcome = await asyncio.wait_for(asyncio.shield(self._catalog_read), CATALOG_WAIT_S)
        except TimeoutError:
            raise ChatRefused("catalog_slow", f"The thread agent's models weren't read within {CATALOG_WAIT_S:g} s",
                              status=503) from None
        if isinstance(outcome, ChatRefused):
            raise outcome
        return outcome

    async def _catalog_outcome(self) -> Catalog | ChatRefused:
        """The catalog, or why not, as a value: a shared read whose waiters
        all gave up still ends without an unretrieved error."""
        try:
            return await self.thread_catalog()
        except ChatRefused as exc:
            return exc

    async def _pin(self, scope: str, scope_id: str) -> dict[str, Any]:
        row = await self.engine.store.read(lambda conn: conn.execute(
            "SELECT agent, model, effort FROM intelligence WHERE scope = ? AND scope_id = ?",
            (scope, scope_id)).fetchone())
        return dict(zip(("agent", "model", "effort"), row)) if row else {}

    async def runtime_for(self, pinned: str | None, purpose: str) -> tuple[str, c.Runtime]:
        """The agent a new session runs on (ch 10 §3.2) and its runtime; it
        must have one and be logged in."""
        agent = pinned or resolve_agent_name()
        runtime = self._runtime(agent, purpose)
        try:
            info = await runtime.describe()
        except c.RuntimeFailure as exc:
            raise ChatRefused(exc.code, f"{agent} can't {purpose} now: {exc.message}", status=409) from exc
        if not info.logged_in:
            raise ChatRefused("auth_required", f"{agent} can't {purpose}: it isn't logged in. Connect it, then try "
                                               "again.", status=409)
        return agent, runtime

    def _runtime(self, agent: str, purpose: str) -> c.Runtime:
        runtime = self.engine.runtime(agent)
        if runtime is None:
            raise ChatRefused("no_runtime", f"{agent} can't {purpose}: it has no storm runtime. Pin an agent that "
                                            "has one.", status=409)
        return runtime

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
    """Start the conversation on a running engine and its review queue: it
    hears the engine's commits (``lifecycle``) and the queue's decisions
    (``conversation``), judges the inputs that ended while it wasn't running
    (``Lifecycle.recover``), and hands a routing a restart cut short to its
    coordinator (``delivery.recover``)."""
    from services.cowork_agent.storm.chats import delivery
    from services.cowork_agent.storm.chats.lifecycle import Lifecycle
    global _chats
    _chats = Chats(engine, queue)
    if queue.conversation is not None:
        queue.conversation.chats = _chats
    project_management.on_removed(_project_removed)
    lifecycle = Lifecycle(_chats)
    engine.subscribe(lifecycle)
    _chats.spawn(lifecycle.recover(), "recover-inputs")
    _chats.spawn(delivery.recover(_chats), "recover-routing")
    return _chats


async def _project_removed(name: str, pid: str | None) -> None:
    """A project removed through XO retires its chat (§0.102); with no
    conversation running there is nothing live to retire now."""
    if _chats is not None:
        await _chats.retire_project(name, pid)


async def shutdown() -> None:
    global _chats
    chats, _chats = _chats, None
    if chats is not None:
        await chats.stop()


def current() -> Chats:
    if _chats is None:
        raise EngineUnavailable("engine_not_running", "Storm's chats aren't running in this server (they start with "
                                                      "the engine); retry shortly.")
    return _chats
