"""The turn engine: runs agent turns for the projects chat, one per session at a time.

Every session's wakes (``wakes.py``) are drained by a turn the server owns,
not by a browser connection: closing the page never stops a turn. A
session runs at most one turn at once (an ``asyncio.Lock`` per session plus
an epoch in its record) and the Space runs at most ``XO_SESSIONS_MAX_TURNS``
turns at once. A wake starts a turn through the enqueue listener, so idle
sessions cost nothing.

A turn:

1. takes every waiting wake of the session and joins their envelopes into
   the turn's input (a thread's first turn: the brief relay, then the owner's
   message);
2. for a thread's first turn, creates its git worktree;
3. issues a fresh comms token and runs the agent through the active
   backend's ``coordinator`` capability (no capability: the projects chat is
   not available for that backend);
4. applies the reply gate: a turn that ended without a successful terminal
   comms call is re-prompted once;
5. records the outcome: the session record and ``events.jsonl``, a session
   index row so the Agents view lists it, a routing decision for a
   coordinator turn, and for a thread turn its status card and, when it
   replied, a status wake for its project coordinator.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.engine import sessions_io
from services.projects_chat import comms, coordinators, envelope, store, wakes, worktree
from services.projects_chat.router import mode
from services.projects_chat.router.decision import Action, Decision
from services.xo_manifest import resolve_agent_name

log = logging.getLogger(__name__)

BATCH_WINDOW_S = 0.3

GATE_TEXT = ("[xo-reply-gate] Your last turn ended without a terminal xo_comms tool call, so nothing reached "
             "anyone: plain text is not delivered. Call the right xo_comms tool now ({tools}), or "
             "no_reply_needed if no reply is warranted.")

#: Comms tool → routing action, for a coordinator turn's decision line.
_ACTION_OF = {
    "start_thread_session": "start_thread", "message_thread": "message_thread", "ask_workspace": "ask_workspace",
    "post_message": "answer", "react": "react", "no_reply_needed": "no_reply",
    "forward_to_project": "forward", "no_such_project": "no_such_project",
}


def comms_url(token: str) -> str:
    base = (os.getenv("XO_COMMS_BASE_URL") or f"http://127.0.0.1:{os.getenv('PORT', '5002')}").rstrip("/")
    return f"{base}/mcp/space-comms/{token}"


class Engine:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._draining: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Bind to the running loop, listen for wakes, and resume what waited."""
        self._loop = asyncio.get_running_loop()
        self._sem = asyncio.Semaphore(mode.max_turns())
        wakes.on_enqueue(self._on_enqueue)
        for project, session_id in wakes.sessions_with_pending():
            self.kick(project, session_id)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _on_enqueue(self, project: Optional[str], session_id: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.kick(project, session_id)
        else:
            loop.call_soon_threadsafe(self.kick, project, session_id)

    def kick(self, project: Optional[str], session_id: str) -> None:
        """Make sure the session's waiting wakes will be run."""
        if self._loop is None:
            return
        existing = self._draining.get(session_id)
        if existing is not None and not existing.done():
            return  # the running drain picks up new wakes before it ends
        task = self._loop.create_task(self._drain(project, session_id), name=f"projects-chat-{session_id}")
        self._draining[session_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def busy(self, session_id: str) -> bool:
        task = self._draining.get(session_id)
        return task is not None and not task.done()

    # ── turns ────────────────────────────────────────────────────────────────
    async def _drain(self, project: Optional[str], session_id: str) -> None:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # Wakes queued together (a tool call that queues several, a burst
            # of messages) share one turn.
            await asyncio.sleep(BATCH_WINDOW_S)
            while wakes.pending(project, session_id):
                session = store.get_session(project, session_id)
                if session is None or session.get("retired_at"):
                    return
                async with self._sem:
                    taken = wakes.take(project, session_id)
                    if not taken:
                        return
                    try:
                        await self.run_turn(project, session, taken)
                    except asyncio.CancelledError:
                        store.update_session(project, session_id, status="interrupted")
                        raise
                    except Exception as exc:  # noqa: BLE001 - one session's failure stays its own
                        log.exception("projects chat: turn failed for %s", session_id)
                        store.update_session(project, session_id, status="failed", last_error=str(exc)[:500])
                        store.append_session_event(project, session_id, "turn.failed", error=str(exc)[:500])
                        return

    async def run_turn(self, project: Optional[str], session: dict, taken: list[dict]) -> dict:
        session_id, role = session["id"], session["role"]
        agent = resolve_agent_name()
        capability = try_load_capability("coordinator", agent=agent)
        if capability is None:
            raise RuntimeError(f"the {agent} backend cannot run the projects chat (no coordinator capability)")
        if role == "thread" and session.get("turns", 0) == 0:
            tree = await worktree.create(project, session["thread_ts"])
            session = store.update_session(project, session_id, cwd=str(tree.path))
            store.update_thread(project, session["thread_ts"], cwd=str(tree.path),
                                worktree_path=str(tree.path) if tree.isolated else None, branch=tree.branch)
        epoch = int(session.get("epoch") or 0) + 1
        session = store.update_session(project, session_id, status="running", epoch=epoch)
        store.append_session_event(project, session_id, "turn.started", epoch=epoch,
                                   wakes=[w["id"] for w in taken])
        prompt = "\n\n".join(w["envelope"] for w in taken)
        result = await self._run(capability, project, session, prompt, resume=session.get("turns", 0) > 0)
        terminal = self._terminal_calls(project, session_id, epoch)
        bounced = False
        if result.get("ok") and not terminal:
            bounced = True
            store.append_session_event(project, session_id, "turn.gate_bounce", epoch=epoch)
            tools = ", ".join(name.split("__")[-1] for name in comms.terminal_tool_names(role))
            result = await self._run(capability, project, session, GATE_TEXT.format(tools=tools), resume=True)
            terminal = self._terminal_calls(project, session_id, epoch)
        session = store.update_session(
            project, session_id, status="idle" if result.get("ok") else "failed",
            turns=int(session.get("turns") or 0) + 1, native_session_id=result.get("native_session_id"),
            last_error=None if result.get("ok") else (result.get("error") or "")[:500],
        )
        store.append_session_event(project, session_id, "turn.finished", epoch=epoch, ok=result.get("ok"),
                                   terminal_tools=terminal, gate_bounced=bounced, outcome=result.get("outcome"),
                                   error=None if result.get("ok") else (result.get("error") or "")[:500])
        self._index_row(project, session, agent)
        if role.endswith("coordinator"):
            self._record_decision(project, session, taken, epoch)
        elif role == "thread":
            self._after_thread_turn(project, session, epoch, result)
        return result

    async def _run(self, capability, project, session, prompt: str, *, resume: bool) -> dict:
        token = store.issue_token(project, session["id"])
        spec = capability.TurnSpec(
            role=session["role"], session_id=session["id"], agent_session_uuid=session["agent_session_uuid"],
            resume=resume, cwd=session["cwd"], prompt=prompt, comms_url=comms_url(token),
            terminal_tools=comms.terminal_tool_names(session["role"]),
            budget_usd=mode.thread_budget_usd() if session["role"] == "thread" else None,
            log_path=str(store._session_dir(project, session["id"]) / "last-turn.jsonl"),
        )
        return await capability.run_turn(spec)

    @staticmethod
    def _turn_calls(project, session_id: str, epoch: int) -> list[dict]:
        """The comms calls made since this epoch's turn started."""
        events = store.read_session_events(project, session_id)
        start = max((i for i, e in enumerate(events) if e.get("type") == "turn.started" and e.get("epoch") == epoch),
                    default=len(events))
        return [e for e in events[start:] if e.get("type") == "comms.call"]

    def _terminal_calls(self, project, session_id: str, epoch: int) -> list[str]:
        """Calls that satisfy the reply gate: a terminal call, or one that handed the work on."""
        return [e["tool"] for e in self._turn_calls(project, session_id, epoch)
                if e.get("ok") and (e.get("terminal") or e.get("tool") in comms.ROUTING)]

    def _index_row(self, project: Optional[str], session: dict, agent: str) -> None:
        key = f"{agent}:{project or ''}:xo:{session['id']}"
        sessions_io.write_session_row(project or "", key, {
            "sessionId": session["id"], "nativeSessionId": session.get("agent_session_uuid"),
            "directory": session.get("cwd"), "backend": agent, "role": session["role"],
            "channel": session.get("channel_id"), "thread_ts": session.get("thread_ts"),
            "updatedAt": int(time.time() * 1000),
        })

    def _record_decision(self, project, session: dict, taken: list[dict], epoch: int) -> None:
        calls = [c for c in self._turn_calls(project, session["id"], epoch) if c.get("ok")]
        actions = []
        for call in calls:
            kind = _ACTION_OF.get(call.get("tool"))
            if kind is None:
                continue
            args, result = call.get("args") or {}, call.get("result") or {}
            target = args.get("thread_id") or args.get("project") or result.get("thread_id")
            actions.append(Action(kind=kind, target=target, title=args.get("title"),
                                  cited=list(args.get("context_message_ids") or ([args["message_id"]]
                                             if args.get("message_id") else []))[:8]))
        trigger = next((w.get("meta", {}).get("message_id") for w in taken if w.get("meta", {}).get("message_id")), None)
        decision = Decision(level="workspace" if session["role"] == "workspace-coordinator" else "project",
                            channel_id=session["channel_id"], trigger_id=trigger or taken[0]["id"],
                            provider="agent", actions=actions)
        try:
            store.record_decision(project, decision.as_dict())
        except Exception:  # noqa: BLE001 - a lost log line must never cost a turn
            log.exception("projects chat: could not record a decision")

    def _after_thread_turn(self, project, session: dict, epoch: int, result: dict) -> None:
        ts = session["thread_ts"]
        calls = [c for c in self._turn_calls(project, session["id"], epoch) if c.get("ok")]
        replies = [c["args"].get("text", "") for c in calls if c.get("tool") == "reply"]
        thread = store.get_thread(project, ts) or {}
        checklist = store.read_status(project, ts) or ""
        open_items = "○" in checklist or "✱" in checklist
        if not result.get("ok"):
            card_state = "failed"
        elif thread.get("state") == "resolved" or (replies and not open_items):
            card_state = "done"
        else:
            card_state = "working"
        happened = (replies[-1] if replies else (result.get("error") or ""))[:140]
        store.write_card(project, ts, {"state": card_state, "headline": thread.get("title"),
                                       "happened": happened, "needs_you": "", "epoch": epoch})
        if thread.get("state") != "resolved" and thread.get("state") != card_state:
            store.update_thread(project, ts, state=card_state)
        if replies or card_state == "failed":
            coordinator = coordinators.ensure(project)
            ch = store.channel(project)
            row = {"id": ts, "author_kind": "system", "author_id": "thread", "written_at": store.now_ms_iso(),
                   "text": f"Thread {thread.get('title')!r} is {card_state}: {happened}"}
            text = envelope.wake(reason="delegation-status", channel_id=ch.id, channel_kind=ch.kind,
                                 trigger=row, thread_ts=ts, trust="peer", mention=False)
            wakes.enqueue(project, coordinator["id"], reason="delegation-status", envelope=text, priority="later")


engine = Engine()


def deliver_owner_message(project: Optional[str], row: dict) -> dict:
    """Route an owner's message: in a thread, straight to that thread's agent;
    on a timeline, to the channel's coordinator. Returns the session woken."""
    ch = store.channel(project)
    if row.get("thread_ts"):
        thread = store.get_thread(project, row["thread_ts"])
        target = store.get_session(project, thread["session_id"])
    else:
        target = coordinators.ensure(project)
    text = envelope.wake(reason="mention", channel_id=ch.id, channel_kind=ch.kind, trigger=row,
                         thread_ts=row.get("thread_ts"))
    wakes.enqueue(project, target["id"], reason="mention", envelope=text, meta={"message_id": row["id"]})
    return target
