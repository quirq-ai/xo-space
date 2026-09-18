"""The runner: one agent session per Inbox item (design sections 17.6 to
17.8).

A session starts through the same path the chat uses, ``AgentDispatcher``'s
stream, with the connection's project as ``agent_id`` and the policy's
``agent_type`` (the ``inbox-item`` skill), so the session index row, the
runtime's own session id, the per-session MCP config and the watcher all
work as for any chat, and this module names no agent. The runner drains
the stream, bounds it by the policy's timeout, parses the last fenced JSON
block of the answer into ``outcome.json``, and records every transition on
the timelines. A manual Start and the auto mode share :func:`start_item`.

One item never has two sessions: ``session.json`` is the lock on disk and
``_running`` the lock in this process. ``XO_INBOX_SESSIONS=off`` stops the
loop; the routes still work, so a person can start one by hand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from services.connections import poller as connections_poller
from services.connections import service as connections_service
from services.cowork_agent import project_layout
from services.periodic import run_forever
from services.timestamps import now_iso
from services.xo_manifest import resolve_agent_name

from . import items
from .store import WorkError

logger = logging.getLogger(__name__)

ENV_ENABLED = "XO_INBOX_SESSIONS"
TICK_S = 15.0
STARTUP_DELAY_S = 5.0
SWEEP_EVERY_S = 3600.0
SUMMARY_MAX = 2000
_OFF = frozenset({"0", "false", "no", "off"})
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)

_running: dict[tuple[str, str], asyncio.Task] = {}
_starts: dict[str, deque] = {}
_last_sweep: float = 0.0


def enabled() -> bool:
    return (os.getenv(ENV_ENABLED, "on") or "on").strip().lower() not in _OFF


def reset_for_tests() -> None:
    global _last_sweep
    _running.clear()
    _starts.clear()
    _last_sweep = 0.0


def running(toolkit: Optional[str] = None) -> list[tuple[str, str]]:
    return sorted(k for k in _running if toolkit is None or k[0] == toolkit)


# ── The pieces a session is made of ─────────────────────────────────────────


def ensure_project(toolkit: str) -> str:
    """The connection's project, scaffolded from the template on first use."""
    project_id = items.project_id_for(toolkit)
    if project_layout.load_project(project_id) is None:
        project_layout.scaffold_project(
            project_id, display_name=f"Inbox: {toolkit}",
            description=f"Sessions that handle items arriving from the {toolkit} connection. Each item works in items/<id>/.")
        logger.info("inbox runner: scaffolded %s", project_id)
    return project_id


def ensure_workbench(toolkit: str, item_id: str) -> Path:
    bench = items.workbench_dir(toolkit, item_id)
    bench.mkdir(parents=True, exist_ok=True)
    return bench


def build_prompt(toolkit: str, record: dict, policy: dict, *, item_file: Path, workbench: Path) -> str:
    act = policy["sessions"]["act"]
    lines = [
        f"You are handling one item that arrived from the {toolkit} connection (collector: {record.get('collector')}).",
        f"Item record, read-only: {item_file}",
        f"Workbench, where your drafts and notes go: {workbench}",
        "Acting on the connection: " + ("yes. Record everything you do under \"acted\"." if act
                                         else "no. Draft only: do not send, reply, label, forward or delete anything."),
        "",
        "The item:",
        f"Title: {record.get('title') or ''}",
        f"Collected at: {record.get('ts') or ''}",
        f"Link: {record.get('url') or 'none'}",
        "Body:",
        (record.get("body") or "(empty)")[: items.BODY_MAX],
        "",
        "When you are done, end your reply with exactly one fenced json block of this shape:",
        "```json",
        '{"kind": "reply_drafted | task_proposed | needs_you | fyi | handled", "summary": "one or two sentences",',
        ' "draft": "a path inside the workbench, or null", "task": {"title": "...", "assignee": null} or null,',
        ' "question": "what you need from the person, or null", "acted": []}',
        "```",
    ]
    return "\n".join(lines)


def parse_outcome(text: str) -> dict:
    """The last fenced ``json`` block of the answer, validated. Raises
    ``ValueError`` with the reason when there is none or it is not an outcome."""
    blocks = _JSON_BLOCK.findall(text or "")
    if not blocks:
        raise ValueError("the answer ended without an outcome block")
    try:
        raw = json.loads(blocks[-1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"the outcome block is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ValueError("the outcome block is not an object")
    kind = raw.get("kind")
    if kind not in items.OUTCOME_KINDS:
        raise ValueError(f"outcome.kind must be one of {list(items.OUTCOME_KINDS)}")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("outcome.summary is required")
    draft = raw.get("draft")
    if draft is not None and (not isinstance(draft, str) or not draft.strip() or ".." in draft.split("/") or draft.startswith("/")):
        raise ValueError("outcome.draft must be a relative path inside the workbench, or null")
    task = raw.get("task")
    if task is not None:
        if not isinstance(task, dict) or not isinstance(task.get("title"), str) or not task["title"].strip():
            raise ValueError("outcome.task must carry a title")
        assignee = task.get("assignee")
        task = {"title": task["title"].strip()[:300], "assignee": assignee.strip() if isinstance(assignee, str) and assignee.strip() else None}
    question = raw.get("question")
    if question is not None and not isinstance(question, str):
        raise ValueError("outcome.question must be a string or null")
    acted = raw.get("acted") or []
    if not isinstance(acted, list) or any(not isinstance(a, str) for a in acted):
        raise ValueError("outcome.acted must be a list of strings")
    return {"kind": kind, "summary": " ".join(summary.split())[:SUMMARY_MAX], "draft": draft.strip() if draft else None,
            "task": task, "question": " ".join(question.split())[:SUMMARY_MAX] if question else None,
            "acted": [a[:300] for a in acted], "at": now_iso()}


REPLY_TEXT_MAX = 4000
_REPLY_HINT = ("\n\n(Answer the person. If what you learn changes your outcome, end with the outcome block again; "
               "otherwise just answer, without a block.)")


def _visible_text(text: str) -> str:
    """The answer without its outcome block: what a person reads on the thread."""
    matches = list(_JSON_BLOCK.finditer(text or ""))
    if not matches:
        return (text or "").strip()
    last = matches[-1]
    return (text[: last.start()] + text[last.end():]).strip()


def check_reply(text) -> str:
    if not isinstance(text, str) or not text.strip():
        raise WorkError("invalid_value", "text is required.")
    if len(text) > REPLY_TEXT_MAX:
        raise WorkError("invalid_value", f"text must be at most {REPLY_TEXT_MAX} chars.")
    return text.strip()


def _open_stream(dispatcher: Any, prompt: str, *, session_id: str, agent_id: str, agent_type: str,
                 user_id: Optional[str], is_new: bool) -> AsyncIterator[dict]:
    """The dispatcher's stream for one turn. One seam, so tests replace it."""
    return dispatcher.stream(prompt, None if is_new else session_id, agent_type=agent_type, our_session_id=session_id,
                             agent_id=agent_id, is_new_session=is_new, user_id=user_id)


def _track(toolkit: str, item_id: str, task: asyncio.Task) -> None:
    """Own the task until it ends. A task cancelled before its first step
    never enters ``_run``, so the session is closed here in that case."""
    _running[(toolkit, item_id)] = task

    def done(finished: asyncio.Task) -> None:
        _running.pop((toolkit, item_id), None)
        if not finished.cancelled():
            return
        session = items.read_session(toolkit, item_id)
        if session and session.get("ended_at") is None:
            _fail(toolkit, item_id, session, session.get("native_session_id"), "cancelled", "the server stopped before the session ran", "")

    task.add_done_callback(done)


def _cap_check(toolkit: str, policy: dict) -> None:
    s = policy["sessions"]
    if len(running(toolkit)) >= s["max_concurrent"]:
        raise WorkError("cap_reached", f"{toolkit} already has {s['max_concurrent']} session(s) running.", 409)
    window = _starts.setdefault(toolkit, deque())
    now = time.monotonic()
    while window and now - window[0] > 3600:
        window.popleft()
    if len(window) >= s["max_per_hour"]:
        raise WorkError("cap_reached", f"{toolkit} reached its {s['max_per_hour']} sessions per hour.", 409)


# ── Start, run, resume ──────────────────────────────────────────────────────


async def start_item(toolkit: str, item_id: str, *, retry: bool = False, manual: bool = True, note: Optional[str] = None) -> dict:
    """Start the item's session. ``retry`` runs a failed item again; ``note``
    is what the person wrote when they started it, added to the prompt and
    to the thread."""
    policy = items.read_policy(toolkit)
    if policy is None:
        raise WorkError("connection_not_configured", f"{toolkit} has no Inbox folder; set its policy first.", 404)
    if policy["sessions"]["mode"] == "off":
        raise WorkError("sessions_off", f"sessions are off for {toolkit}.", 409)
    record = items.require_item(toolkit, item_id)
    if record.get("decided"):
        raise WorkError("item_decided", "this item was already decided.", 409)
    if (toolkit, item_id) in _running:
        raise WorkError("session_exists", "a session is running for this item.", 409)
    previous = items.read_session(toolkit, item_id)
    if previous is not None and not retry:
        raise WorkError("session_exists", "this item already had a session; Retry runs it again.", 409)
    if retry and record.get("status") != "failed":
        raise WorkError("not_failed", "only a failed item can be retried.", 409)
    _cap_check(toolkit, policy)
    project_id = await asyncio.to_thread(ensure_project, toolkit)
    workbench = await asyncio.to_thread(ensure_workbench, toolkit, item_id)
    session_id = str(uuid.uuid4())
    runtime = policy["sessions"]["runtime"] or resolve_agent_name()
    session = {"session_id": session_id, "native_session_id": None, "runtime": runtime, "project_id": project_id,
               "workbench": f"items/{item_id}", "agent_type": policy["sessions"]["agent_type"], "started_at": now_iso(),
               "ended_at": None, "exit": None, "attempt": (previous or {}).get("attempt", 0) + 1, "manual": manual}
    await asyncio.to_thread(items.write_session, toolkit, item_id, session)
    record = await asyncio.to_thread(items.update_item, toolkit, item_id, status="running", session_id=session_id, outcome=None)
    _starts.setdefault(toolkit, deque()).append(time.monotonic())
    items.record_event(toolkit, "inbox.item.started", record, session_id=session_id, runtime=runtime, status="running")
    prompt = build_prompt(toolkit, record, policy, item_file=items.item_path(toolkit, item_id), workbench=workbench)
    if note:
        await asyncio.to_thread(items.append_thread, toolkit, item_id, "person", note, session_id=session_id, attempt=session["attempt"])
        prompt += "\n\nThe person adds, before you start:\n" + note
    _track(toolkit, item_id, asyncio.create_task(_run(toolkit, item_id, policy, session, prompt, is_new=True)))
    logger.info("inbox runner: started %s/%s (session %s, attempt %d)", toolkit, item_id, session_id[:8], session["attempt"])
    return {"session_id": session_id, "attempt": session["attempt"], "project_id": project_id}


async def _resume(toolkit: str, item_id: str, policy: dict, session: dict, prompt: str, *, person_text: str,
                  require_outcome: bool, fallback_status: str) -> dict:
    """Continue the item's session with one more turn. The person's words go
    on the thread first; the runner then drains the resumed stream."""
    if (toolkit, item_id) in _running:
        raise WorkError("session_running", "the agent is still answering; wait for it.", 409)
    _cap_check(toolkit, policy)
    session = {**session, "ended_at": None, "exit": None, "resumed_at": now_iso()}
    await asyncio.to_thread(items.write_session, toolkit, item_id, session)
    await asyncio.to_thread(items.append_thread, toolkit, item_id, "person", person_text,
                            session_id=session["session_id"], attempt=session.get("attempt"))
    record = await asyncio.to_thread(items.update_item, toolkit, item_id, status="running")
    _starts.setdefault(toolkit, deque()).append(time.monotonic())
    items.record_event(toolkit, "inbox.item.started", record, session_id=session["session_id"], runtime=session.get("runtime"), status="running")
    _track(toolkit, item_id, asyncio.create_task(_run(toolkit, item_id, policy, session, prompt, is_new=False,
                                                       require_outcome=require_outcome, fallback_status=fallback_status)))
    return {"session_id": session["session_id"]}


async def reply_item(toolkit: str, item_id: str, text: str) -> dict:
    """The person writes on the item's thread. An item with no session yet
    starts one with the note; an item that has one resumes it, and the
    agent may answer without an outcome block."""
    text = check_reply(text)
    policy = items.read_policy(toolkit)
    if policy is None:
        raise WorkError("connection_not_configured", f"{toolkit} has no Inbox folder; set its policy first.", 404)
    if policy["sessions"]["mode"] == "off":
        raise WorkError("sessions_off", f"sessions are off for {toolkit}.", 409)
    record = items.require_item(toolkit, item_id)
    if (toolkit, item_id) in _running:
        raise WorkError("session_running", "the agent is still answering; wait for it.", 409)
    session = items.read_session(toolkit, item_id)
    if session is None:
        return await start_item(toolkit, item_id, retry=record.get("status") == "failed", manual=True, note=text)
    previous = record.get("status") if record.get("status") in ("done", "failed") else "failed"
    return await _resume(toolkit, item_id, policy, session, text + _REPLY_HINT, person_text=text,
                         require_outcome=False, fallback_status=previous)


async def send_item(toolkit: str, item_id: str) -> dict:
    """Resume a drafted item's session with the instruction to send."""
    policy = items.read_policy(toolkit)
    if policy is None:
        raise WorkError("connection_not_configured", f"{toolkit} has no Inbox folder.", 404)
    if not policy["sessions"]["act"]:
        raise WorkError("act_not_allowed", f"{toolkit} sessions may draft but not act; allow it in the policy first.", 409)
    record = items.require_item(toolkit, item_id)
    outcome = items.read_outcome(toolkit, item_id)
    session = items.read_session(toolkit, item_id)
    if record.get("status") != "done" or not outcome or outcome.get("kind") != "reply_drafted" or session is None:
        raise WorkError("no_draft", "this item has no drafted reply to send.", 409)
    prompt = ("The person approved your draft" + (f" at {outcome.get('draft')}" if outcome.get("draft") else "")
              + ". Send it now through the connection, then end with the outcome block with kind \"handled\" and what you did under \"acted\".")
    return await _resume(toolkit, item_id, policy, session, prompt, person_text="Send the draft.",
                         require_outcome=True, fallback_status="done")


async def _run(toolkit: str, item_id: str, policy: dict, session: dict, prompt: str, *, is_new: bool,
               require_outcome: bool = True, fallback_status: str = "failed") -> None:
    parts: list[str] = []
    native: Optional[str] = None
    error: Optional[str] = None
    try:
        from services.cowork_agent.engine.dispatcher import AgentDispatcher

        user_id = await connections_poller.resolve_user_id()
        dispatcher = AgentDispatcher(session["runtime"])

        async def drain() -> None:
            nonlocal native, error
            async for ev in _open_stream(dispatcher, prompt, session_id=session["session_id"], agent_id=session["project_id"],
                                         agent_type=session["agent_type"], user_id=user_id, is_new=is_new):
                if not isinstance(ev, dict):
                    continue
                if ev.get("done"):
                    native = ev.get("native_session_id") or native
                    break
                if ev.get("type") == "token":
                    parts.append(str(ev.get("token") or ""))
                elif ev.get("type") == "error":
                    error = str(ev.get("error") or "stream error")

        await asyncio.wait_for(drain(), timeout=policy["sessions"]["timeout_s"])
        if error:
            raise RuntimeError(error)
        text = "".join(parts)
        try:
            outcome: Optional[dict] = parse_outcome(text)
        except ValueError:
            if require_outcome:
                raise
            outcome = None   # a follow-up answer need not restate the outcome
        await asyncio.to_thread(_finish, toolkit, item_id, session, native, outcome, text)
    except asyncio.TimeoutError:
        await asyncio.to_thread(_fail, toolkit, item_id, session, native, "timeout",
                                f"timed out after {policy['sessions']['timeout_s']} s", "".join(parts), item_status=fallback_status)
    except asyncio.CancelledError:
        await asyncio.to_thread(_fail, toolkit, item_id, session, native, "cancelled", "the server stopped while the session ran",
                                "".join(parts), item_status=fallback_status)
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the item, never raised into the loop
        await asyncio.to_thread(_fail, toolkit, item_id, session, native, "error", str(exc)[:300], "".join(parts), item_status=fallback_status)
    finally:
        _running.pop((toolkit, item_id), None)


def _finish(toolkit: str, item_id: str, session: dict, native: Optional[str], outcome: Optional[dict], text: str) -> None:
    if outcome is not None:
        items.write_outcome(toolkit, item_id, outcome)
    items.write_session(toolkit, item_id, {**session, "native_session_id": native or session.get("native_session_id"),
                                           "ended_at": now_iso(), "exit": {"status": "ok", "message": None}})
    items.write_log(toolkit, item_id, text)
    kind = outcome["kind"] if outcome is not None else (items.read_item(toolkit, item_id) or {}).get("outcome")
    record = items.update_item(toolkit, item_id, status="done", outcome=kind)
    visible = _visible_text(text) or (outcome or {}).get("summary") or ""
    items.append_thread(toolkit, item_id, "agent", visible, session_id=session["session_id"], attempt=session.get("attempt"),
                        outcome=outcome["kind"] if outcome is not None else None)
    # the line says what this turn did: a new outcome, or just an answer
    items.record_event(toolkit, "inbox.item.finished", record, session_id=session["session_id"], runtime=session.get("runtime"),
                       status=outcome["kind"] if outcome is not None else "replied")
    logger.info("inbox runner: finished %s/%s: %s", toolkit, item_id, outcome["kind"] if outcome is not None else "replied")


def _fail(toolkit: str, item_id: str, session: dict, native: Optional[str], status: str, message: str, text: str, *,
          item_status: str = "failed") -> None:
    items.write_session(toolkit, item_id, {**session, "native_session_id": native or session.get("native_session_id"),
                                           "ended_at": now_iso(), "exit": {"status": status, "message": message}})
    items.write_log(toolkit, item_id, text)
    record = items.update_item(toolkit, item_id, status=item_status)
    items.append_thread(toolkit, item_id, "system", f"The session {status}: {message}", session_id=session["session_id"],
                        attempt=session.get("attempt"))
    items.record_event(toolkit, "inbox.item.failed", record, session_id=session["session_id"], runtime=session.get("runtime"), status=status)
    logger.warning("inbox runner: %s/%s failed: %s", toolkit, item_id, message)


# ── The loop ────────────────────────────────────────────────────────────────


async def _auto_start(toolkit: str, policy: dict) -> int:
    kinds = set(policy["sessions"]["kinds"])
    if policy["sessions"]["mode"] != "auto" or not kinds:
        return 0
    started = 0
    idx = await asyncio.to_thread(items.read_index, toolkit)
    candidates = sorted(((s.get("ts") or "", item_id) for item_id, s in idx["items"].items()
                         if s.get("status") == "new" and not s.get("decided") and s.get("collector") in kinds))
    for _ts, item_id in candidates:
        try:
            await start_item(toolkit, item_id, manual=False)
            started += 1
        except WorkError as exc:
            if exc.code == "cap_reached":
                break
            logger.warning("inbox runner: could not start %s/%s: %s", toolkit, item_id, exc.message)
    return started


def _harvest_orphans(toolkit: str) -> int:
    """Items the index says are running but no task in this process owns:
    a server restart. They become failed, and Retry brings them back."""
    orphaned = 0
    for item_id, s in items.read_index(toolkit)["items"].items():
        if s.get("status") not in ("running", "queued") or (toolkit, item_id) in _running:
            continue
        session = items.read_session(toolkit, item_id)
        if session is None:
            items.update_item(toolkit, item_id, status="failed")
            continue
        _fail(toolkit, item_id, session, session.get("native_session_id"), "orphaned", "the server restarted while the session ran", "")
        orphaned += 1
    return orphaned


async def tick() -> dict:
    global _last_sweep
    summary = {"connections": 0, "made": 0, "started": 0, "orphaned": 0, "swept": 0}
    now = time.monotonic()
    do_sweep = now - _last_sweep >= SWEEP_EVERY_S
    for toolkit in await asyncio.to_thread(items.list_connections):
        summary["connections"] += 1
        try:
            summary["made"] += len(await asyncio.to_thread(items.make_items, toolkit))
            summary["orphaned"] += await asyncio.to_thread(_harvest_orphans, toolkit)
            policy = await asyncio.to_thread(items.read_policy, toolkit)
            if policy is not None:
                summary["started"] += await _auto_start(toolkit, policy)
            if do_sweep:
                summary["swept"] += await asyncio.to_thread(items.sweep, toolkit)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one connection must not stop the others
            logger.warning("inbox runner: %s failed this tick", toolkit, exc_info=True)
    if do_sweep:
        _last_sweep = now
    if summary["made"] or summary["started"] or summary["orphaned"] or summary["swept"]:
        logger.info("inbox runner: %s", summary)
    return summary


async def _after_poll(toolkit: str) -> None:
    """A "Poll now" that collected something makes its items at once."""
    if toolkit not in items.list_connections():
        return
    await asyncio.to_thread(items.make_items, toolkit)
    policy = items.read_policy(toolkit)
    if policy is not None and enabled():
        await _auto_start(toolkit, policy)


async def start_inbox_runner() -> None:
    """Entry point for the background task, beside the other pollers."""
    if not enabled():
        logger.info("inbox runner: disabled by %s", ENV_ENABLED)
        return
    connections_service.register_new_events_listener(_after_poll)
    await run_forever("inbox runner", tick, interval_s=lambda: TICK_S, startup_delay_s=STARTUP_DELAY_S,
                      enabled=enabled, logger=logger)
