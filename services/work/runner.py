"""The runner: one agent session per Inbox work item (docs/work-and-workitems.md
section 18).

A session starts through the same path the chat uses, ``AgentDispatcher``'s
stream, with the work item's project as ``agent_id`` and the section policy's
``agent_type`` (the ``inbox-item`` skill), so the session index row, the
runtime's own session id, the per-session MCP config and the watcher all
work as for any chat, and this module names no agent. The runner claims the
work item for the turn (``workitems/claims.json``, what the Inbox and the
work item routes read as "in progress"), links the session to the item,
drains the stream bounded by the policy's timeout, parses the last fenced
JSON block of the answer into ``outcome.json``, releases the claim, and
closes the work item when the outcome says the fact is handled.

One work item never has two sessions at once: ``session.json`` is the lock
on disk and ``_running`` the lock in this process. ``XO_INBOX_SESSIONS=off``
stops the loop; the routes still work, so a person can start one by hand.
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
from services.inbox import facts
from services.inbox import service as inbox_service
from services.periodic import run_forever
from services.timestamps import now_iso
from services.xo_manifest import resolve_agent_name

from . import inbox_view, items
from .store import WorkError

logger = logging.getLogger(__name__)

ENV_ENABLED = "XO_INBOX_SESSIONS"
TICK_S = 15.0
STARTUP_DELAY_S = 5.0
SWEEP_EVERY_S = 3600.0
SUMMARY_MAX = 2000
_OFF = frozenset({"0", "false", "no", "off"})
_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)

Key = tuple[str, str]   # (project_id, workitem_id)

_running: dict[Key, asyncio.Task] = {}
_starts: dict[str, deque] = {}
_last_sweep: float = 0.0


def enabled() -> bool:
    return (os.getenv(ENV_ENABLED, "on") or "on").strip().lower() not in _OFF


def reset_for_tests() -> None:
    global _last_sweep
    _running.clear()
    _starts.clear()
    _last_sweep = 0.0


def running(section: Optional[str] = None) -> list[Key]:
    """The items whose session this process is driving now."""
    if section is None:
        return sorted(_running)
    return sorted(k for k in _running if items.section_of(items.require_record(*k), items.read_fact(*k)) == section)


# ── The pieces a session is made of ─────────────────────────────────────────


def ensure_workbench(project_id: str, workitem_id: str) -> Path:
    bench = items.workbench_dir(project_id, workitem_id)
    bench.mkdir(parents=True, exist_ok=True)
    return bench


def build_prompt(section: str, record: dict, fact: dict, policy: dict, *, fact_file: Optional[Path], workbench: Path) -> str:
    act = policy["sessions"]["act"]
    toolkit = ((fact.get("source") or {}).get("connection") or {}).get("toolkit") if isinstance(fact.get("source"), dict) else None
    origin = f"the {toolkit} connection" if toolkit else f"the {section} section of the Inbox"
    lines = [
        f"You are handling one Inbox work item from {origin} (source: {(record.get('source') or {}).get('kind')}, kind: {fact.get('kind')}).",
        f"Work item: {record.get('id')} in project {record.get('_project_id') or fact.get('project_id') or ''}",
        f"Fact record, read-only: {fact_file or 'not on disk'}",
        f"Workbench, where your drafts and notes go: {workbench}",
        "Acting on the connection: " + ("yes. Record everything you do under \"acted\"." if act
                                         else "no. Draft only: do not send, reply, label, forward or delete anything."),
        "",
        "The item:",
        f"Title: {record.get('title') or fact.get('title') or ''}",
        f"Collected at: {fact.get('ts') or ''}",
        f"Link: {fact.get('url') or 'none'}",
        f"Project: {fact.get('project_id') or 'none'}",
        f"Opens in the Space: {json.dumps(fact.get('link')) if fact.get('link') else 'nothing'}",
        "Body:",
        (fact.get("body") or "(empty)")[: facts.BODY_MAX],
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
               "otherwise just answer.)")


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


def _track(key: Key, task: asyncio.Task) -> None:
    """Own the task until it ends. A task cancelled before its first step
    never enters ``_run``, so the session is closed here in that case."""
    _running[key] = task

    def done(finished: asyncio.Task) -> None:
        _running.pop(key, None)
        if not finished.cancelled():
            return
        session = items.read_session(*key)
        if session and session.get("ended_at") is None:
            _fail(key, session, session.get("native_session_id"), "cancelled", "the server stopped before the session ran", "")

    task.add_done_callback(done)


def _cap_check(section: str, policy: dict) -> None:
    s = policy["sessions"]
    if len(running(section)) >= s["max_concurrent"]:
        raise WorkError("cap_reached", f"{section} already has {s['max_concurrent']} session(s) running.", 409)
    window = _starts.setdefault(section, deque())
    now = time.monotonic()
    while window and now - window[0] > 3600:
        window.popleft()
    if len(window) >= s["max_per_hour"]:
        raise WorkError("cap_reached", f"{section} reached its {s['max_per_hour']} sessions per hour.", 409)


def _claim(key: Key, session_id: str, runtime: str) -> None:
    try:
        items.scope_for(key[0]).claim_workitem(key[1], session_id=session_id, runtime=runtime)
    except Exception:  # noqa: BLE001 - the claim is the Inbox's "in progress"; a session must not fail for it
        logger.warning("inbox runner: could not claim %s/%s", *key, exc_info=True)


def _release(key: Key) -> None:
    try:
        items.scope_for(key[0]).release_workitem_quiet(key[1])
    except Exception:  # noqa: BLE001 - see _claim
        logger.warning("inbox runner: could not release %s/%s", *key, exc_info=True)


# ── Start, run, resume ──────────────────────────────────────────────────────


async def start_item(project_id: str, workitem_id: str, *, retry: bool = False, manual: bool = True,
                     note: Optional[str] = None) -> dict:
    """Start the item's session. ``retry`` runs a failed item again; ``note``
    is what the person wrote when they started it, added to the prompt."""
    key = (project_id, workitem_id)
    record = items.require_record(project_id, workitem_id)
    fact = items.read_fact(project_id, workitem_id) or {}
    section = items.section_of(record, fact)
    policy = items.read_policy(section)
    if policy["sessions"]["mode"] == "off":
        raise WorkError("sessions_off", f"sessions are off for the {section} section.", 409)
    if record.get("status") == "closed":
        raise WorkError("item_closed", "this work item is closed; reopen it first.", 409)
    if key in _running:
        raise WorkError("session_exists", "a session is running for this item.", 409)
    previous = items.read_session(project_id, workitem_id)
    if previous is not None and not retry:
        raise WorkError("session_exists", "this item already had a session; Retry runs it again.", 409)
    if retry and previous is not None and previous.get("ended_at") is None:
        raise WorkError("session_exists", "the session is still running.", 409)
    if retry and previous is not None and ((previous.get("exit") or {}).get("status") in (None, "ok")):
        raise WorkError("not_failed", "only a failed item can be retried.", 409)
    _cap_check(section, policy)
    workbench = await asyncio.to_thread(ensure_workbench, project_id, workitem_id)
    session_id = str(uuid.uuid4())
    runtime = policy["sessions"]["runtime"] or resolve_agent_name()
    session = {"session_id": session_id, "native_session_id": None, "runtime": runtime, "project_id": project_id,
               "workitem_id": workitem_id, "section": section, "workbench": f"items/{workitem_id}",
               "agent_type": policy["sessions"]["agent_type"], "started_at": now_iso(), "ended_at": None, "exit": None,
               "attempt": (previous or {}).get("attempt", 0) + 1, "manual": manual}
    await asyncio.to_thread(items.write_session, project_id, workitem_id, session)
    await asyncio.to_thread(items.link_session, project_id, workitem_id, session_id)
    await asyncio.to_thread(_claim, key, session_id, runtime)
    _starts.setdefault(section, deque()).append(time.monotonic())
    items.record_event(project_id, "inbox.item.started", workitem_id=workitem_id, title=record.get("title") or workitem_id,
                       section=section, session_id=session_id, runtime=runtime, status="running")
    prompt = build_prompt(section, {**record, "_project_id": project_id}, fact, policy,
                          fact_file=facts.fact_path(project_id, workitem_id), workbench=workbench)
    if note:
        prompt += "\n\nThe person adds, before you start:\n" + note
    _track(key, asyncio.create_task(_run(key, section, policy, session, prompt, is_new=True)))
    logger.info("inbox runner: started %s/%s (session %s, attempt %d)", project_id, workitem_id, session_id[:8], session["attempt"])
    return {"session_id": session_id, "attempt": session["attempt"], "project_id": project_id, "workitem_id": workitem_id}


async def _resume(key: Key, section: str, policy: dict, session: dict, prompt: str, *, require_outcome: bool) -> dict:
    """Continue the item's session with one more turn."""
    if key in _running:
        raise WorkError("session_running", "the agent is still answering; wait for it.", 409)
    _cap_check(section, policy)
    session = {**session, "ended_at": None, "exit": None, "resumed_at": now_iso()}
    await asyncio.to_thread(items.write_session, *key, session)
    await asyncio.to_thread(_claim, key, session["session_id"], session.get("runtime") or resolve_agent_name())
    _starts.setdefault(section, deque()).append(time.monotonic())
    items.record_event(key[0], "inbox.item.started", workitem_id=key[1], title=items.require_record(*key).get("title") or key[1],
                       section=section, session_id=session["session_id"], runtime=session.get("runtime"), status="running")
    _track(key, asyncio.create_task(_run(key, section, policy, session, prompt, is_new=False, require_outcome=require_outcome)))
    return {"session_id": session["session_id"], "project_id": key[0], "workitem_id": key[1]}


async def reply_item(project_id: str, workitem_id: str, text: str) -> dict:
    """The person writes to the agent. An item with no session yet starts
    one with the note; an item that has one resumes it, and the agent may
    answer without an outcome block."""
    text = check_reply(text)
    key = (project_id, workitem_id)
    record = items.require_record(project_id, workitem_id)
    fact = items.read_fact(project_id, workitem_id) or {}
    section = items.section_of(record, fact)
    policy = items.read_policy(section)
    if policy["sessions"]["mode"] == "off":
        raise WorkError("sessions_off", f"sessions are off for the {section} section.", 409)
    if key in _running:
        raise WorkError("session_running", "the agent is still answering; wait for it.", 409)
    session = items.read_session(project_id, workitem_id)
    if session is None:
        return await start_item(project_id, workitem_id, manual=True, note=text)
    if session.get("ended_at") is None:
        raise WorkError("session_running", "the session is still running.", 409)
    return await _resume(key, section, policy, session, text + _REPLY_HINT, require_outcome=False)


async def send_item(project_id: str, workitem_id: str) -> dict:
    """Resume a drafted item's session with the instruction to send."""
    key = (project_id, workitem_id)
    record = items.require_record(project_id, workitem_id)
    fact = items.read_fact(project_id, workitem_id) or {}
    section = items.section_of(record, fact)
    policy = items.read_policy(section)
    if not policy["sessions"]["act"]:
        raise WorkError("act_not_allowed", f"{section} sessions may draft but not act; allow it in the policy first.", 409)
    outcome = items.read_outcome(project_id, workitem_id)
    session = items.read_session(project_id, workitem_id)
    if record.get("status") == "closed" or not outcome or outcome.get("kind") != "reply_drafted" or session is None \
            or session.get("ended_at") is None:
        raise WorkError("no_draft", "this item has no drafted reply to send.", 409)
    prompt = ("The person approved your draft" + (f" at {outcome.get('draft')}" if outcome.get("draft") else "")
              + ". Send it now through the connection, then end with the outcome block with kind \"handled\" and what you did under \"acted\".")
    return await _resume(key, section, policy, session, prompt, require_outcome=True)


async def _run(key: Key, section: str, policy: dict, session: dict, prompt: str, *, is_new: bool,
               require_outcome: bool = True) -> None:
    parts: list[str] = []
    native: Optional[str] = None
    error: Optional[str] = None
    try:
        from services.cowork_agent.engine.dispatcher import AgentDispatcher

        user_id = await connections_poller.resolve_user_id()
        dispatcher = AgentDispatcher(session["runtime"])

        async def drain() -> None:
            nonlocal native, error
            async for ev in _open_stream(dispatcher, prompt, session_id=session["session_id"], agent_id=key[0],
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
        await asyncio.to_thread(_finish, key, section, session, native, outcome)
    except asyncio.TimeoutError:
        await asyncio.to_thread(_fail, key, session, native, "timeout", f"timed out after {policy['sessions']['timeout_s']} s", "".join(parts))
    except asyncio.CancelledError:
        await asyncio.to_thread(_fail, key, session, native, "cancelled", "the server stopped while the session ran", "".join(parts))
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the item, never raised into the loop
        await asyncio.to_thread(_fail, key, session, native, "error", str(exc)[:300], "".join(parts))
    finally:
        _running.pop(key, None)


def _finish(key: Key, section: str, session: dict, native: Optional[str], outcome: Optional[dict]) -> None:
    project_id, workitem_id = key
    if outcome is not None:
        items.write_outcome(project_id, workitem_id, outcome)
    items.write_session(project_id, workitem_id, {**session, "native_session_id": native or session.get("native_session_id"),
                                                  "ended_at": now_iso(), "exit": {"status": "ok", "message": None}})
    _release(key)
    record = items.require_record(project_id, workitem_id)
    kind = outcome["kind"] if outcome is not None else None
    if kind in items.CLOSING_OUTCOMES and record.get("status") != "closed":
        try:
            record = items.update_record(project_id, workitem_id, status="closed", state_reason="completed")
        except WorkError as exc:
            logger.warning("inbox runner: %s/%s handled but could not close: %s", project_id, workitem_id, exc.message)
    items.record_event(project_id, "inbox.item.finished", workitem_id=workitem_id, title=record.get("title") or workitem_id,
                       section=section, session_id=session["session_id"], runtime=session.get("runtime"),
                       status=kind or "replied")
    logger.info("inbox runner: finished %s/%s: %s", project_id, workitem_id, kind or "replied")


def _fail(key: Key, session: dict, native: Optional[str], status: str, message: str, text: str) -> None:
    project_id, workitem_id = key
    items.write_session(project_id, workitem_id, {**session, "native_session_id": native or session.get("native_session_id"),
                                                  "ended_at": now_iso(), "exit": {"status": status, "message": message}})
    _release(key)
    try:
        record = items.require_record(project_id, workitem_id)
    except WorkError:
        record = {}
    items.record_event(project_id, "inbox.item.failed", workitem_id=workitem_id, title=record.get("title") or workitem_id,
                       section=session.get("section") or items.section_of(record, items.read_fact(project_id, workitem_id)),
                       session_id=session["session_id"], runtime=session.get("runtime"), status=status)
    logger.warning("inbox runner: %s/%s failed: %s", project_id, workitem_id, message)


# ── The loop ────────────────────────────────────────────────────────────────


async def _auto_start(section: str, policy: dict) -> int:
    """Every new work item of the section gets its session; ``kinds``
    narrows that to the listed fact kinds. Caps stop the loop for this tick."""
    if policy["sessions"]["mode"] != "auto":
        return 0
    started = 0
    candidates = await asyncio.to_thread(inbox_view.candidates, section, kinds=policy["sessions"]["kinds"])
    for project_id, workitem_id, _fact in candidates:
        try:
            await start_item(project_id, workitem_id, manual=False)
            started += 1
        except WorkError as exc:
            if exc.code == "cap_reached":
                break
            logger.warning("inbox runner: could not start %s/%s: %s", project_id, workitem_id, exc.message)
    return started


def _harvest_orphans() -> int:
    """Sessions whose sidecar says running but no task in this process owns:
    a server restart. They become failed, and Retry brings them back."""
    orphaned = 0
    for row in inbox_view.workitem_rows(_running):
        session = row.get("session")
        key = (row["project_id"], row["id"])
        if not session or session.get("ended_at") is not None or key in _running:
            continue
        full = items.read_session(*key)
        if full is None:
            continue
        _fail(key, full, full.get("native_session_id"), "orphaned", "the server restarted while the session ran", "")
        orphaned += 1
    return orphaned


async def tick() -> dict:
    global _last_sweep
    summary = {"created": 0, "started": 0, "orphaned": 0, "swept": 0}
    now = time.monotonic()
    do_sweep = now - _last_sweep >= SWEEP_EVERY_S
    summary["created"] = await asyncio.to_thread(_refresh_inbox, False)
    try:
        summary["orphaned"] = await asyncio.to_thread(_harvest_orphans)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a bad sidecar must not stop the tick
        logger.warning("inbox runner: the orphan harvest failed this tick", exc_info=True)
    for section in items.SECTIONS:
        try:
            policy = await asyncio.to_thread(items.read_policy, section)
            summary["started"] += await _auto_start(section, policy)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one section must not stop the others
            logger.warning("inbox runner: %s failed this tick", section, exc_info=True)
    if do_sweep:
        try:
            summary["swept"] = await asyncio.to_thread(items.sweep)
        except Exception:  # noqa: BLE001 - retention can wait an hour
            logger.warning("inbox runner: the sweep failed", exc_info=True)
        _last_sweep = now
    if any(summary.values()):
        logger.info("inbox runner: %s", summary)
    return summary


def _refresh_inbox(force: bool) -> int:
    """Pull the feeders so work items arrive without anyone opening the
    Inbox; the inbox service throttles the read itself."""
    try:
        return inbox_service.refresh(force=force)
    except Exception:  # noqa: BLE001 - a failing feeder must not stop the tick
        logger.warning("inbox runner: the inbox refresh failed", exc_info=True)
        return 0


async def _after_poll(toolkit: str) -> None:
    """A "Poll now" that collected something starts its sessions at once."""
    await asyncio.to_thread(_refresh_inbox, True)
    if enabled():
        await _auto_start("connections", await asyncio.to_thread(items.read_policy, "connections"))


async def start_inbox_runner() -> None:
    """Entry point for the background task, beside the other pollers."""
    if not enabled():
        logger.info("inbox runner: disabled by %s", ENV_ENABLED)
        return
    connections_service.register_new_events_listener(_after_poll)
    await run_forever("inbox runner", tick, interval_s=lambda: TICK_S, startup_delay_s=STARTUP_DELAY_S,
                      enabled=enabled, logger=logger)
