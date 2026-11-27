"""Try once: one read-only run of an agent, before you rely on it.

``start`` opens a thread on the ``try`` preset (the coordinators' read-only
tool set, comms kept; a runtime that can't enforce it refuses through the
preset's ``requires``), the way a job's fire opens its run: a system root
message marked ``extra.try``, then the person's task as their own message
in the thread, which wakes it. What is tried is one of:

* a saved agent, ``(name, level)``, resolved as a run resolves it;
* an unsaved form, the file's ``text``, checked as a saved file is;
* an open review card: an agent's proposal (an ``approval`` whose detail
  holds ``create_persona``: its file, at its level), or a ``create_job`` card's job (its prompt, runtime, model, effort
  and agent). The card stays open: a try decides nothing.

A try only reads, so its worktree is kept for nothing: ``Tries`` hears its
session go cold once the thread has finished and removes the tree as the
person's own removal would, never forcing it (a tree that would lose work
stays in the worktrees list, for the person to look at). It starts listening
with the first try a server opens, and then looks once for finished tries
whose trees are still there (a restart in between).
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from dataclasses import replace
from typing import Any, Mapping, Sequence

from services.cowork_agent.storm import personas
from services.cowork_agent.storm.chats import Chats, envelope, rows
from services.cowork_agent.storm.chats.delivery import OWNER
from services.cowork_agent.storm.engine import Engine, Wake, Writes
from services.cowork_agent.storm.jobs.runs import FINISHED, JobRefused, project_chat
from services.cowork_agent.storm.presets import Intelligence
from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.worktrees import service as worktrees
from services.errors import ServiceError

log = logging.getLogger(__name__)

#: The root message of a try's thread.
LABEL = "Try: {name}"
#: The task a try runs when none is given.
DEFAULT_TASK = "Say in a few lines what you do and how you would go about it, then hand back."
#: A try's root message's ``extra`` key (thread rows read it, as they read ``job``).
EXTRA = "try"


class TryRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400, what: Any = None) -> None:
        super().__init__(code, message, status=status)
        if what is not None:
            self.what = what


def problems_view(found: Sequence[Any]) -> list[dict[str, Any]]:
    return [{"line": p.line, "key": p.key, "message": p.message} for p in found]


def _runnable(persona: Any, found: Sequence[Any]) -> Any:
    if persona is None or found:
        raise TryRefused("invalid_persona", "This agent's file has mistakes; fix them before trying it.",
                         what=problems_view(found))
    return persona


async def _parsed(text: str, level: str) -> Any:
    """An unsaved file as the agent it would be at ``level``."""
    persona, found = await asyncio.to_thread(personas.parse, text)
    return replace(_runnable(persona, found), level=level)


async def _resolved(project: str | None, name: str, level: str) -> Any:
    persona = await asyncio.to_thread(personas.resolve, project, name, level)
    if persona is None:
        raise TryRefused("persona_not_found", f"There is no {level} agent {name!r}.", status=404)
    return _runnable(persona, persona.problems)


async def _from_card(chats: Chats, review_id: str, project: str | None) -> tuple[dict, Any, str | None, dict]:
    """``(chat, persona, task, job)`` an open card holds; a Space agent's
    card names no project, so its try runs in ``project``."""
    item = await chats.queue.get_item(review_id)
    detail = item["detail"]
    proposed = detail.get("create_persona") if item["kind"] == "approval" else None
    if proposed is None and item["kind"] != "create_job":
        raise TryRefused("not_tryable", "This card holds nothing to try; only a proposed agent or job.")
    if item["state"] != "open":
        raise TryRefused("card_closed", "This card was decided already; try the saved agent or job instead.",
                         status=409)
    if proposed is not None:
        where = proposed["project"] or project
        if where is None:
            raise TryRefused("project_required", "Name the project the try runs in.")
        return await chats.project(where), await _parsed(proposed["file"], proposed["level"]), None, {}
    job = detail["job"]
    chat = await project_chat(chats, job["chat"])
    named = job.get("persona")
    persona = await _resolved(chat["project"], named["name"], named["level"]) if named else None
    return chat, persona, job["prompt"], job


async def start(chats: Chats, *, project: str | None, task: str | None, persona: Mapping[str, str] | None = None,
                text: str | None = None, review_id: str | None = None) -> dict[str, str]:
    """Open a try of exactly one of ``persona``, ``text`` or ``review_id``;
    ``{chat_id, ts}`` of its thread."""
    job: dict[str, Any] = {}
    if review_id is not None:
        chat, chosen, given, job = await _from_card(chats, review_id, project)
        task = task or given
    else:
        if project is None:
            raise TryRefused("project_required", "Name the project the try runs in.")
        chat = await chats.project(project)
        chosen = (await _resolved(project, persona["name"], persona["level"]) if persona is not None
                  else await _parsed(text or "", personas.PROJECT))
    chats.writable(chat)
    if not await asyncio.to_thread(Chats.folder(chat).is_dir):
        raise JobRefused("project_missing", f"The project folder {chat['project']!r} is gone, so a try has "
                                            "nowhere to read.", status=409)
    name = chosen.name if chosen is not None else job["name"]
    ts = rows.new_id("cmsg")

    def root(w: Writes) -> None:
        Chats.writable(rows.get_chat(w, chat["id"]))         # removed meanwhile, it runs nothing
        rows.add_message(w, chat_id=chat["id"], author_kind="system", author_id="xo", message_id=ts,
                         text=LABEL.format(name=name), extra={EXTRA: True})

    def also(w: Writes, session_id: str, _note: str | None) -> None:
        said = rows.add_message(w, chat_id=chat["id"], thread_ts=ts, author_kind="owner", author_id=OWNER,
                                text=task or DEFAULT_TASK)
        w.wake(session_id, Wake(id=f"try:{ts}", priority="now", reason="message", origin="person",
                                text=envelope.wake(reason="mention", chat=chat, trigger=said, now=w.now,
                                                   thread_ts=ts)))
    _listen(chats.engine)
    await chats.start_thread(chat, ts, LABEL.format(name=name), Intelligence(job.get("model"), job.get("effort")),
                             also, agent=job.get("agent"), preset="try", root=root, chosen_by="try",
                             persona=chosen)
    return {"chat_id": chat["id"], "ts": ts}


# ── a finished try's worktree ────────────────────────────────────────────

#: A finished try with its tree still there: (chat, ts) of each, or of one when ``ts`` is given.
_LEFT = ("SELECT t.chat_id, t.ts FROM threads t JOIN messages m ON m.id = t.ts "
         "WHERE json_extract(m.extra, '$.try') AND t.worktree_path IS NOT NULL AND t.deleted_at IS NULL "
         "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.type = 'worktree.removed' AND +e.chat_id = t.chat_id "
         "AND +e.thread_ts = t.ts)")


class Tries:
    """An engine listener: a try's session went cold, so its tree may go."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._tasks: set[asyncio.Task] = set()

    def committed(self, events: Sequence[StoredEvent]) -> None:
        for event in events:
            if event.type == "session" and event.data.get("status") == "cold" and event.thread_ts:
                self._spawn(self.tidy(event.chat_id, event.thread_ts))

    def ephemeral(self, event: Any, epoch: int) -> None:
        """Nothing live matters here."""

    async def tidy(self, chat_id: str | None = None, ts: str | None = None) -> None:
        """Remove the tree of each finished try (of ``ts``, else all)."""
        def read(conn) -> list[tuple[str, str]]:
            found = conn.execute(_LEFT + (" AND t.chat_id = ? AND t.ts = ?" if ts else ""),
                                 (chat_id, ts) if ts else ()).fetchall()
            return [(c, t) for c, t in found if rows.get_thread(conn, c, t)["state"] in FINISHED]
        for chat, thread in await self.engine.store.read(read):
            try:
                await worktrees.current().remove(chat, thread)
            except ServiceError as exc:     # still busy (the next cold event retries), or it would lose work: kept
                log.info("storm tries: %s's worktree stays for now (%s): %s", thread, exc.code, exc.message)
            except Exception:  # noqa: BLE001 - a tree left is harmless; the trace goes to the log
                log.exception("storm tries: removing %s's worktree failed", thread)

    def _spawn(self, work: Any) -> None:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


_listening: "weakref.WeakSet[Engine]" = weakref.WeakSet()


def _listen(engine: Engine) -> None:
    """Hear this engine's sessions from the first try on, and tidy what a restart left."""
    if engine in _listening:
        return
    _listening.add(engine)
    tries = Tries(engine)
    engine.subscribe(tries)
    tries._spawn(tries.tidy())
