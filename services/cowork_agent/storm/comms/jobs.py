"""The job tools, for every role (design ch 12 §5.8; ch 11 §14.6; §0.107).

* ``create_job`` only asks: it opens a ``create_job`` review item with the
  exact title, schedule and prompt in plain words, and no job exists until
  the person allows it (``storm/jobs`` registers it then). Its project,
  agent, model and effort are checked first, so the agent hears a mistake
  at once, with what it may choose instead. A title or prompt holding a
  secret is refused: the item is stored redacted (ch 16 §3), so what the
  person reads, allows and registers would differ.
* ``list_jobs`` reads the agent jobs, which are timers (``storm/jobs``);
  ``cancel_job`` turns one off at once, with no approval (it only narrows
  work), recording who and why, in the call's own transaction.

**A job always names a project** (§0.107). A thread or a project
coordinator schedules in its own project; the workspace coordinator, and a
plain chat outside a project, must name one, or are refused with the list of
projects. A plain chat (the person's own) may name any project.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Mapping

from services.cowork_agent.storm import chats as chats_pkg
from services.cowork_agent.storm.chats import check_pin, rows
from services.cowork_agent.storm.comms import args as a
from services.cowork_agent.storm.comms.calls import CommsError, ToolCall, current_turn
from services.cowork_agent.storm.comms.tools import EFFORTS, ID_MAX, NOTE_MAX, TIME_MAX, WORD_MAX
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.places import project_of
from services.cowork_agent.storm.jobs import SECRET, created_by
from services.cowork_agent.storm.jobs.schedule import describe
from services.cowork_agent.storm.records import timers
from services.cowork_agent.storm.records.timers import Timer
from services.cowork_agent.storm.redact import redact
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.review.tiers import Tier
from utils.commands import scheduler

#: How many project names a refusal lists.
PROJECTS_LISTED = 30
CALENDAR = ("Jobs repeat at a fixed interval from first_run_at: every is {seconds: N}, or null to run once. "
            "Calendar rules (weekdays only, month ends) can't be expressed; for every Monday at 19:00, use "
            "604800 seconds with first_run_at on a Monday at 19:00.")
GUIDANCE = "Nothing is scheduled until the owner allows it; their answer arrives as a new wake. Keep working."


async def _own_project(t: ToolCall) -> str | None:
    """The project the caller works in, if any: its chat's, or, for a plain
    session, the project folder it runs in."""
    caller = t.caller
    if caller.role == "plain":
        return await asyncio.to_thread(lambda: project_of(caller.cwd).name or None)
    chat = await t.chats.chat(caller.chat_id)
    return chat["project"] if chat["kind"] == rows.PROJECT else None


def _listing(names: list[str]) -> str:
    if not names:
        return "there are none yet"
    more = len(names) - PROJECTS_LISTED
    return ", ".join(names[:PROJECTS_LISTED]) + (f", and {more} more" if more > 0 else "")


async def _scope(t: ToolCall, named: str | None, *, required: bool) -> str | None:
    """The project a call is about: the one named, else the caller's own.
    Only a plain chat may name a project other than its own."""
    own = await _own_project(t)
    if named is None:
        if own is None and required:
            names = await asyncio.to_thread(chats_pkg.project_folders)
            raise CommsError("project_required", f"A job always runs in a project, and you have none of your own: "
                                                 f"name one with project ({_listing(names)}).")
        return own
    if own is not None and named != own and t.caller.role != "plain":
        raise CommsError("not_your_project", f"A {t.caller.role} works with jobs in its own project only, {own}.")
    names = await asyncio.to_thread(chats_pkg.project_folders)
    if named not in names:
        raise CommsError("no_such_project", f"There is no project {named!r}. Projects: {_listing(names)}.")
    return named


def _every(args: Mapping[str, Any]) -> int | None:
    if "every" not in args:
        raise CommsError("missing_argument", "every is required: {seconds: N} to repeat, or null to run once.")
    every = args["every"]
    if every is None:
        return None
    if not isinstance(every, Mapping) or set(every) != {"seconds"}:
        raise CommsError("invalid_schedule", CALENDAR)
    seconds = every["seconds"]
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise CommsError("invalid_schedule", "every.seconds is a whole number of seconds.")
    least = scheduler.min_every_seconds()
    if seconds < least:
        raise CommsError("invalid_schedule", f"every.seconds must be at least {least}: the scheduler looks once "
                                             "per tick, so a shorter interval can't be kept.")
    return seconds


async def create_job(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    title = a.text(t.args, "title", limit=scheduler.NAME_MAX)
    prompt = a.text(t.args, "prompt", limit=scheduler.PROMPT_MAX)
    first_run_at = a.text(t.args, "first_run_at", limit=TIME_MAX)
    named = a.text(t.args, "project", required=False, limit=ID_MAX)
    agent = a.text(t.args, "agent", required=False, limit=WORD_MAX)
    model = a.text(t.args, "model", required=False, limit=WORD_MAX)
    effort = a.choice(t.args, "effort", EFFORTS)
    every = _every(t.args)
    if not re.fullmatch(scheduler.NAME_PATTERN, title):
        raise CommsError("invalid_argument", "title is 1-64 characters: letters, digits, space, '_', '.', '-', "
                                             "starting with a letter or digit.")
    for field, value in (("title", title), ("prompt", prompt)):
        if redact(value) != value:
            raise CommsError("secret_in_job", SECRET.format(field=field))
    project = await _scope(t, named, required=True)
    chat = await t.chats.project(project)
    t.chats.writable(chat)                  # a retired chat runs nothing (§0.102): never ask for its job
    payload = {"kind": scheduler.KIND_AGENT, "name": title, "chat": chat["id"], "prompt": prompt,
               "every_seconds": every, "first_run_at": first_run_at, "agent": agent, "model": model, "effort": effort}
    try:
        job = scheduler.validate_definition(payload)
    except ValueError as exc:
        raise CommsError("invalid_schedule", f"{exc}.") from exc
    _agent, runtime = await t.chats.runtime_for(agent, "run this job")
    await check_pin(runtime, {}, {"model": model, "effort": effort})
    when = describe(job["every_seconds"], job["first_run_at"], job["utc_offset"])
    chosen = ", ".join(part for part in (agent, model and f"model {model}", effort and f"effort {effort}") if part)
    where = f"{project} with {chosen}" if chosen else project

    def fn(w: Writes) -> dict[str, Any]:
        t.chats.writable(rows.get_chat(w, chat["id"]))        # read again: it may have been retired meanwhile
        item = t.chats.queue.open_item(w, ItemSpec(
            kind="create_job", title=f"Schedule “{title}” in {where}: {when[:1].lower()}{when[1:]}",
            origin="agent", session_id=caller.session_id, chat_id=caller.chat_id, thread_ts=caller.thread_ts,
            turn_id=current_turn(w, caller.session_id), epoch=caller.epoch, risk=Tier.NOTABLE,
            risk_reasons=("runs an agent in a project on a schedule",),
            detail={"text": f"{when} in {where}: {prompt}", "schedule": when, "project": project, "job": payload}))
        return t.record(w, {"review_id": item, "state": "open", "guidance": GUIDANCE})
    return await t.engine.write(fn)


def _projects(w: Writes) -> dict[str, str]:
    """Each project chat's folder name, by chat id."""
    return dict(w.execute("SELECT id, project_name FROM chats WHERE kind = 'project'").fetchall())


def _view(timer: Timer, project: str | None) -> dict[str, Any]:
    definition, last = timer.definition, timer.last
    armed = timer.state == "armed"
    return {"job_id": timer.id, "title": definition["name"], "project": project,
            "every": {"seconds": timer.every_s} if timer.every_s else None,
            "schedule": describe(timer.every_s, definition.get("first_run_at"),
                                 definition.get("utc_offset") or "+00:00"),
            "next_run_at": timer.due_at if armed else None, "enabled": armed,
            "last_run": {"at": last.get("at"), "status": last.get("status"), "thread_id": last.get("thread_ts"),
                         "reason": last.get("reason")} if last else None,
            "created_by": created_by(timer)}


async def list_jobs(t: ToolCall) -> dict[str, Any]:
    named = a.text(t.args, "project", required=False, limit=ID_MAX)
    project = await _scope(t, named, required=False)

    def fn(w: Writes) -> dict[str, Any]:
        names = _projects(w)
        return {"jobs": [_view(timer, names.get(timer.chat_id)) for timer in timers.jobs(w)
                         if project is None or names.get(timer.chat_id) == project]}
    return await t.write(fn)


async def cancel_job(t: ToolCall) -> dict[str, Any]:
    caller = t.caller
    job_id = a.text(t.args, "job_id", limit=ID_MAX)
    reason = a.text(t.args, "reason", limit=NOTE_MAX)
    project = await _scope(t, None, required=False) if caller.role != "plain" else None

    def fn(w: Writes) -> dict[str, Any]:
        timer = timers.get(w, job_id)
        chat = rows.get_chat(w, timer.chat_id) if timer is not None and timer.job is not None else None
        if chat is None or (project is not None and chat.get("project") != project):
            raise CommsError("no_such_job", f"There is no job {job_id!r} you can reach; list_jobs shows them.")
        timers.end(w, timer, state="cancelled", reason=f"Turned off by session {caller.session_id}: {reason}",
                   actor=caller.session_id)
        return {"ok": True, "job_id": job_id, "enabled": False}
    return await t.write(fn)
