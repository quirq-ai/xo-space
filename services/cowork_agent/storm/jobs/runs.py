"""A job's fire opens a thread, once (design ch 10 §4.5, ch 11 §7.1, ch 07
§4.1).

The fire's key is its wake id, ``sched:<job>:<fire time>``, unique in the
whole store. One transaction writes the run's system root message, the
thread and its session on the ``job`` preset, and that wake, whose input is
the job's prompt as the owner's words. A duplicate tick, a manual run in the
same second and a restart that hands the fire over again find the thread
the first one opened, by that key: wherever the job's chat is now (the
person may have moved the job to another project, or the project may have
been removed since), a fire that opened a thread reports that one. The wake
is pruned 30 days after it is consumed, so the root message also keeps the
key (its ``client_id``) and is the fallback, looked up in the job's chat. One
corner remains, negligible: a fire is pending only until a tick hands it over
again with storm running. After a restart with the watcher or the scheduler
turned off, that can be longer than 30 days, so a second thread needs both
that and the job moved to another chat meanwhile.

The run's agent is the job's (else the active one) and must have a runtime
and be logged in, or the run fails saying so. A model or effort the agent no
longer offers is dropped for this run, said in its thread, and the thread
preset's default used: the job keeps running rather than failing each time
until someone edits it. When the agent's models can't be read at the fire,
the run likewise uses the default and says why, rather than losing the fire.

A fire while the job's previous run is still working opens nothing: it is
recorded ``skipped`` (§0.117), as the scheduler skips a command job's fire
while its last run goes on, so a run blocked on a review item or a usage
limit doesn't stack a thread, worktree and wake per fire. The previous run
is the job's latest run thread, wherever its chat is now. It is still
working while its session takes inputs (not retired: a removed project's
runs nothing more) and either its thread is not done, failed or resolved
(its agent is on it, or it waits on the person: a reply, a decision, a
pause) or its session is busy: on an input, or one waits for it (as when
the person writes to a finished run). The next fire runs once it is neither.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from services.cowork_agent.storm.chats import ChatRefused, Chats, check_pin, envelope, rows
from services.cowork_agent.storm.chats.lifecycle import WORKING
from services.cowork_agent.storm.engine import Wake, Writes
from services.cowork_agent.storm.jobs.schedule import fire_label
from services.cowork_agent.storm.presets import Intelligence
from services.cowork_agent.storm.redact import redact
from services.errors import ServiceError
from services.xo_manifest import resolve_agent_name

#: A run's thread waits behind the person's work (ch 07 §4.1).
PRIORITY = "later"
#: What a run's thread says when the job's model or effort is gone.
DROPPED = "{why} This run uses the thread's default instead; edit the job to choose again."
#: What a run's thread says when the agent's models can't be read to check the job's.
UNCHECKED = "The job's model and effort couldn't be checked, so this run uses the thread's default: {why}"
#: Why a fire opened nothing (§0.117).
STILL_WORKING = "the previous run is still working"
#: A run thread's states once it has finished; any other is still working.
FINISHED = ("done", "failed", "resolved")


class JobRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(code, message, status=status)


class _FireOpened(Exception):
    """Another hand-over of this fire committed its thread first."""


class _StillWorking(Exception):
    """The job's previous run is still working (§0.117)."""


def wake_id(job_id: str, fire_at: str) -> str:
    return f"sched:{job_id}:{fire_at}"


async def project_chat(chats: Chats, chat_id: str) -> dict[str, Any]:
    """The project chat a job runs in: a job always names a project
    (§0.107), and a retired chat runs nothing (§0.102)."""
    try:
        chat = await chats.chat(chat_id)
    except ChatRefused as exc:
        if exc.code != "chat_not_found":
            raise
        raise JobRefused("no_such_chat", f"There is no chat {chat_id!r}; a job runs in a project's chat "
                                         "(GET /api/chat/chats lists them).") from exc
    if chat["kind"] != rows.PROJECT:
        raise JobRefused("not_a_project_chat", "A job always runs in a project: name a project's chat, not the "
                                               "workspace's.")
    chats.writable(chat)
    return chat


async def fitting(runtime: Any, wanted: Intelligence) -> tuple[Intelligence, list[str]]:
    """``wanted`` without what the runtime no longer offers, and why each was dropped."""
    model, effort, why = wanted.model, wanted.effort, []
    for _attempt in range(2):           # a model, then an effort
        try:
            await check_pin(runtime, {}, {"model": model, "effort": effort})
            break
        except ChatRefused as exc:
            if exc.code == "unsupported_model":
                model = None
            elif exc.code == "unsupported_effort":
                effort = None
            else:                       # unreadable now: the fire runs rather than being lost, as a routed thread
                return Intelligence(), [UNCHECKED.format(why=exc.message)]
            why.append(DROPPED.format(why=exc.message))
    return Intelligence(model, effort), why


def _fired(conn, key: str, chat_id: str) -> tuple[str, str] | None:
    """``(chat, thread)`` the fire ``key`` already opened: its wake's session's,
    wherever it is; else, once the wake is pruned, its root message's in the
    job's chat ``chat_id``."""
    found = conn.execute("SELECT s.chat_id, s.thread_ts FROM wakes w JOIN sessions s ON s.id = w.session_id "
                         "WHERE w.id = ?", (key,)).fetchone()
    return found or conn.execute("SELECT chat_id, id FROM messages WHERE chat_id = ? AND client_id = ?",
                                 (chat_id, key)).fetchone()


def _still_working(conn, job_id: str) -> bool:
    """Whether ``job_id``'s latest run is still working (the module's
    docstring says when). Its runs' root messages carry their fire's key."""
    first = wake_id(job_id, "")
    after = first[:-1] + ";"            # ";" follows ":": every key of the job sorts between the two
    latest = conn.execute(
        "SELECT t.state, s.status, s.retired_at IS NOT NULL, EXISTS (SELECT 1 FROM wakes k WHERE k.session_id = s.id "
        "AND k.state IN ('queued', 'taken')) FROM sessions s JOIN threads t ON t.chat_id = s.chat_id AND "
        "t.ts = s.thread_ts JOIN messages m ON m.id = t.ts WHERE s.preset = 'job' AND m.client_id >= ? AND "
        "m.client_id < ? ORDER BY m.seq DESC LIMIT 1", (first, after)).fetchone()
    if latest is None:
        return False
    state, status, retired, inputs = latest
    return not retired and (state not in FINISHED or status in WORKING or bool(inputs))


def _opened(chat_id: str, ts: str, *, already: bool = False) -> dict[str, Any]:
    return {"status": "ok", "chat_id": chat_id, "thread_ts": ts, **({"already": True} if already else {})}


def _skipped() -> dict[str, Any]:
    return {"status": "skipped", "reason": STILL_WORKING}


async def open_run(chats: Chats, job: Mapping[str, Any], fire_at: str, trigger: str) -> dict[str, Any]:
    """Open the thread of ``job``'s fire at ``fire_at``, or find the one
    already open. Returns the scheduler's outcome; raises a ``ServiceError``
    saying why the run can't open."""
    engine = chats.engine
    key = wake_id(job["id"], fire_at)
    found = await engine.store.read(lambda conn: _fired(conn, key, job["chat"]))
    if found is not None:
        return _opened(*found, already=True)
    chat = await project_chat(chats, job["chat"])
    if not await asyncio.to_thread(Chats.folder(chat).is_dir):
        raise JobRefused("project_missing", f"The project folder {chat['project']!r} is gone, so this job's run "
                                            "has nowhere to work.", status=409)
    if await engine.store.read(lambda conn: _still_working(conn, job["id"])):
        return _skipped()                   # before its worktree is made
    runtime = engine.runtime(job.get("agent") or resolve_agent_name())
    intelligence, dropped = (await fitting(runtime, Intelligence(job.get("model"), job.get("effort")))
                             if runtime is not None else (Intelligence(None, None), []))
    ts = rows.new_id("cmsg")
    review = (job.get("approved_by") or {}).get("review_id")
    made = {"id": job["id"], "fire_at": fire_at, "trigger": trigger, **({"review_id": review} if review else {})}

    def root(w: Writes) -> None:
        if _fired(w, key, chat["id"]) is not None:
            raise _FireOpened
        # The project may have been removed while the run was being made (§0.102).
        Chats.writable(rows.get_chat(w, chat["id"]))
        if _still_working(w, job["id"]):
            raise _StillWorking
        rows.add_message(w, chat_id=chat["id"], author_kind="system", author_id="xo", client_id=key, message_id=ts,
                         text=fire_label(job["name"], fire_at, job.get("utc_offset") or "+00:00"),
                         extra={"job": made})

    def also(w: Writes, session_id: str, _note: str | None) -> None:
        for note in dropped:
            rows.add_message(w, chat_id=chat["id"], thread_ts=ts, author_kind="system", author_id="xo", text=note)
        def quoting(prompt: str) -> str:
            return envelope.schedule(chat=chat, thread_ts=ts, job={**job, "prompt": prompt}, fire_at=fire_at,
                                     now=w.now)
        # The prompt is escaped in the envelope, where a secret holding & < >
        # no longer matches: the consumed wake keeps one quoting it redacted.
        w.wake(session_id, Wake(id=key, priority=PRIORITY, reason="schedule", text=quoting(job["prompt"]),
                                kept=quoting(redact(job["prompt"]))))
    try:
        await chats.start_thread(chat, ts, job["name"], intelligence, also, agent=job.get("agent"), preset="job",
                                 root=root, chosen_by="job")
    except _FireOpened:
        return _opened(*await engine.store.read(lambda conn: _fired(conn, key, chat["id"])), already=True)
    except _StillWorking:
        return _skipped()
    return _opened(chat["id"], ts)
