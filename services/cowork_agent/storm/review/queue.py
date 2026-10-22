"""The review queue: everything only the person can settle (design ch 15
§5–§9; ch 07 §5.1, §6; ch 11 §10).

It is the engine's ``ReviewDesk``. A held CLI call (a permission the policy
said to ask about, a CLI's own question) opens an item in the same
transaction as the session's ``waiting_on_person``, with its
``review.opened`` event; a pool moving onto paid usage opens a ``usage``
item. The conversation layer's asks (B3) and the integrity check (B5) open
theirs through ``open_item``.

**Deciding** (``decide``) is the only writer of an owner decision, and the
server sets ``decided_by``. A decision is single-use: the item moves from
``open`` to ``decided`` in the one transaction that records it, and a retry
with the same ``idempotency_key`` gets the same result without writing
again. The decision is bound to the exact action it was asked for (its
canonical hash is recorded; a grant matches only that). Delivery: straight
into a call still held; a grant and a ``now`` wake once the call is gone
(parked, or a dead epoch); a deny wake with the note; a ``review-answered``
wake for an ask. A secret answer goes into its waiting call or nowhere.

**Expiry:** an ask with no held call expires 7 days after it opened, a
parked call 7 days after parking, a held call never; the asker gets a
``review-expired`` wake (``later``). One timer, at the earliest expiry,
retried with a backoff when a pass fails. A ``usage`` item expires once the
limit it asked about has reset.

**The loop breaker** (``loops.py``) counts the policy's denials, heard from
the permission steps the engine records, and the person's denials, in the
transaction that records each.

The parts of an item that need a thread or a coordinator (the thread's
``needs_approval``, the ``ask`` hop and the coordinator's ``next`` wake, the
relay when the person pauses a thread, the ``review-answered`` envelope)
belong to the conversation layer (``storm/chats/conversation.py``), which
plugs in as ``Conversation``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid
from collections import OrderedDict
from typing import Any, Awaitable, Mapping, Protocol, Sequence

from services.cowork_agent.storm.review import engine_link as link
from services.cowork_agent.storm.review import loops, rule_store
from services.cowork_agent.storm.review.decisions import Plan, Request, ReviewRefused, plan
from services.cowork_agent.storm.review.items import (
    COLUMNS,
    DECIDED_BY,
    EXPIRING_KINDS,
    EXPIRY,
    ItemSpec,
    expiry,
    fixed_options,
    permission_options,
    view,
)
from services.cowork_agent.storm.review.rule_store import match_of
from services.cowork_agent.storm.review.rules import Match
from services.timestamps import parse_ts

log = logging.getLogger(__name__)

#: Permission requests remembered until their answer (the loop breaker's
#: view of "which action was denied"); a bound, not a cap on anything.
ASKED_REMEMBERED = 1024
PAUSED_BY_PERSON = "paused by the person after repeated denials"
#: After a failed expiry pass: 1, 2, 4 … 300 s until the next try.
EXPIRY_RETRY_FIRST_S = 1.0
EXPIRY_RETRY_MAX_S = 300.0


class Conversation(Protocol):
    """The conversation layer's part of an item (chats and threads, B3).
    Each runs inside the queue's transaction."""

    def opened(self, w: link.Writes, item: Mapping[str, Any]) -> None:
        """The thread's ``needs_approval``; the ``ask`` hop and ``next`` wake to its project coordinator."""

    def closed(self, w: link.Writes, item: Mapping[str, Any]) -> None:
        """Decided, expired or withdrawn: the thread's state moves on."""

    def paused(self, w: link.Writes, item: Mapping[str, Any]) -> None:
        """The person confirmed a suggestion: the relay to the coordinator, so it can start fresh."""

    def answered(self, w: link.Writes, item: Mapping[str, Any], text: str, option: str | None) -> str:
        """The ``review-answered`` wake's text for the session that asked (ch 11 §4)."""


class ReviewQueue:
    def __init__(self, conversation: Conversation | None = None) -> None:
        self.conversation = conversation
        self.engine: link.Engine | None = None
        self._deciding = asyncio.Lock()          # decisions and expiry, one at a time
        self._asked: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self._timer = None
        self._due: str | None = None
        self._expiry_retry = 0.0
        self._closed = False

    # ── life ─────────────────────────────────────────────────────────────

    async def start(self, engine: link.Engine) -> None:
        """Once the engine runs: hear its commits, and arm the expiry timer."""
        self.engine = engine
        link.subscribe(engine, self)
        self._arm(await link.read(engine, _next_expiry))

    async def stop(self) -> None:
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── the engine's desk (its transaction, on the store's thread) ───────

    def open(self, w: link.Writes, ask: link.Ask) -> str:
        if ask.kind == "usage":
            return self._open_usage(w, ask)
        spec = self._permission(w, ask) if ask.kind == "permission" else self._question(w, ask)
        return self.open_item(w, spec)

    def park(self, w: link.Writes, item_id: str, reason: str) -> None:
        expires = expiry(w.now)
        if w.execute("UPDATE review_items SET parked = 1, expires_at = ? WHERE id = ? AND state = 'open'",
                     (expires, item_id)).rowcount:
            w.append("review.parked", {"id": item_id, "reason": reason, "expires_at": expires}, **_ids(w, item_id))

    def withdraw(self, w: link.Writes, item_id: str, reason: str) -> None:
        if w.execute("UPDATE review_items SET state = 'withdrawn' WHERE id = ? AND state = 'open'",
                     (item_id,)).rowcount:
            w.append("review.withdrawn", {"id": item_id, "reason": reason}, **_ids(w, item_id))
            self._closed_item(w, item_id)

    def supersede(self, w: link.Writes, session_id: str, *, by: str, call: link.CallRef | None = None) -> list[str]:
        """One action, one item (the live run's D1): withdraw the session's
        open, parked items as superseded, so no decision on them can mint a
        grant. With ``call`` (a new item just opened for it, ``by``): only
        the parked items asked for the same call. Without (``by`` is the
        person's new message): all of them."""
        rows = w.execute("SELECT id, kind, json_extract(detail, '$.input_sha256') FROM review_items "
                         "WHERE session_id = ? AND state = 'open' AND parked = 1 AND id <> ?",
                         (session_id, by)).fetchall()
        old = [item for item, kind, digest in rows
               if call is None or (kind == call.kind and digest == call.input_sha256)]
        for item in old:
            w.execute("UPDATE review_items SET state = 'withdrawn' WHERE id = ? AND state = 'open'", (item,))
            w.append("review.withdrawn", {"id": item, "reason": "superseded", "by": by}, **_ids(w, item))
            self._closed_item(w, item)
        return old

    def open_item(self, w: link.Writes, spec: ItemSpec) -> str:
        """Open an item of any kind, with its ``review.opened`` event, in the
        caller's transaction."""
        item_id = f"rv_{uuid.uuid4().hex[:16]}"
        expires = spec.expires if spec.expires is not None else spec.kind in EXPIRING_KINDS and spec.native is None
        row = [item_id, None, spec.kind, "open", spec.risk.name.lower() if spec.risk is not None else None,
               json.dumps(list(spec.risk_reasons)), int(spec.blocking), spec.title, spec.why,
               json.dumps(dict(spec.detail), default=str),
               json.dumps(list(spec.options) if spec.options is not None else fixed_options(spec.kind)),
               spec.chat_id, spec.thread_ts, spec.session_id, spec.run_id, spec.turn_id, spec.epoch, spec.origin, 0,
               w.now, expiry(w.now) if expires else None, None, None, None, None, "[]",
               json.dumps(dict(spec.native)) if spec.native is not None else None]
        item = view(row)
        item.pop("seq")
        # The state log never keeps a CLI's raw input (ch 08 §4): the item does, to show it.
        logged = {**item, "detail": {k: v for k, v in item["detail"].items() if k != "input"}}
        row[1] = w.append("review.opened", logged, chat_id=spec.chat_id, session_id=spec.session_id,
                          thread_ts=spec.thread_ts, turn_id=spec.turn_id, run_id=spec.run_id, epoch=spec.epoch)
        w.execute(f"INSERT INTO review_items ({COLUMNS}) VALUES ({', '.join('?' * len(row))})", row)
        if self.conversation is not None:
            self.conversation.opened(w, {**item, "seq": row[1]})
        return item_id

    def _permission(self, w: link.Writes, ask: link.Ask) -> ItemSpec:
        req, verdict, call = ask.request, ask.verdict, ask.call
        role = w.execute("SELECT role FROM sessions WHERE id = ?", (ask.session_id,)).fetchone()
        chat = w.execute("SELECT project_pid, project_name FROM chats WHERE id = ?", (ask.chat_id,)).fetchone() \
            if ask.chat_id else None
        revoked = frozenset(r[0] for r in w.execute("SELECT id FROM default_rule_revocations").fetchall())
        options = permission_options(
            kind=req.kind, tier=verdict.tier, assessment=verdict.assessment, revoked=revoked,
            coordinator=bool(role) and role[0].endswith("coordinator"), thread_ts=ask.thread_ts,
            project_pid=chat[0] if chat else None, project_name=(chat[1] or chat[0]) if chat else None)
        run = _run(ask)
        detail = {"tool": req.tool, "kind": req.kind, "command": req.command, "paths": list(req.paths),
                  "url": req.url, "mcp": list(req.mcp) if req.mcp else None, "cwd": req.cwd,
                  "input": dict(req.input), "input_sha256": call.input_sha256, "run": run}
        return ItemSpec(kind="permission", title=_title(req.summary, run, "wants to"), origin="cli",
                        risk=verdict.tier, risk_reasons=verdict.reasons, blocking=True, why=req.reason,
                        detail=detail, options=options,
                        native={"request_id": req.request_id, "scopes": list(req.scopes),
                                "call": dataclasses.asdict(call)}, **_where(ask))

    def _question(self, w: link.Writes, ask: link.Ask) -> ItemSpec:
        q = ask.question
        run = _run(ask)
        return ItemSpec(kind="question", title=_title(ask.title, run, "asks:"), origin="cli", blocking=True,
                        detail={"questions": [dict(x) for x in q.questions], "run": run,
                                "input_sha256": ask.call.input_sha256},
                        native={"request_id": q.request_id, "call": dataclasses.asdict(ask.call)}, **_where(ask))

    def _open_usage(self, w: link.Writes, ask: link.Ask) -> str:
        """One open ``usage`` item per pool. One whose reset has passed asks
        about a limit that is over: it expires and a new one opens."""
        resets = w.execute("SELECT resets_at FROM pools WHERE id = ?", (ask.pool,)).fetchone()
        known = w.execute("SELECT id, json_extract(detail, '$.resets_at') FROM review_items WHERE kind = 'usage' "
                          "AND state = 'open' AND json_extract(detail, '$.pool') = ?", (ask.pool,)).fetchone()
        if known and not (known[1] and known[1] <= w.now):
            return known[0]
        if known:
            self._expire_usage(w, known[0])
        return self.open_item(w, ItemSpec(kind="usage", title=ask.title, origin="engine", blocking=True,
                                          detail={"pool": ask.pool, "resets_at": resets[0] if resets else None}))

    def _expire_usage(self, w: link.Writes, item_id: str) -> None:
        if w.execute("UPDATE review_items SET state = 'expired' WHERE id = ? AND state = 'open'",
                     (item_id,)).rowcount:
            w.append("review.expired", {"id": item_id, "kind": "usage"})
            self._closed_item(w, item_id)

    # ── deciding ─────────────────────────────────────────────────────────

    async def decide(self, item_id: str, req: Request) -> dict[str, Any]:
        """The person's decision (the decision route is the only caller)."""
        engine = self._engine()
        async with self._deciding:
            row = await link.read(engine, lambda conn: conn.execute(
                f"SELECT {COLUMNS} FROM review_items WHERE id = ?", (item_id,)).fetchone())
            if row is None:
                raise _not_found(item_id)
            item, native = view(row), json.loads(row[-1]) if row[-1] else None
            if item["state"] == "decided":
                made = item["decision"]
                if made.get("idempotency_key") != req.idempotency_key:
                    raise ReviewRefused("already_decided", f"{item_id} was already decided "
                                                           f"({made.get('decision')}).", status=409)
                if made.get("request_sha256") != req.digest(item):
                    raise ReviewRefused("idempotency_key_reused", "This idempotency_key was used for a different "
                                                                  "decision on this item.", status=409)
                return item
            if item["state"] == "withdrawn":
                raise ReviewRefused("withdrawn", "The agent took this request back; there is nothing to decide.",
                                    status=409)
            now = link.iso(link.clock(engine).time())
            if item["state"] == "expired" or (item["expires_at"] and item["expires_at"] <= now):
                raise ReviewRefused("expired", f"{item_id} expired unanswered; the agent was told.", status=410)
            past = item["detail"].get("resets_at") if item["kind"] == "usage" else None
            if past and past <= now:
                # It asked about a limit that has since reset: never answered late.
                await link.write(engine, lambda w: self._expire_usage(w, item_id))
                raise ReviewRefused("expired", f"{item_id} asked about a usage limit that has reset since; XO asks "
                                               "again if the account is still on paid usage.", status=410)
            await self._apply(engine, item, native, plan(item, native, req))
        return await self.get_item(item_id)

    async def _apply(self, engine: link.Engine, item: dict, native: Mapping | None, p: Plan) -> None:
        paused: list[str] = []

        def record(w: link.Writes, delivery: str | None) -> int:
            if p.secret and delivery != "in_call":
                # The call is gone: the answer would wait in a wake, stored. Nothing is written.
                raise ReviewRefused("secret_needs_a_call", "The agent's question is no longer waiting, so a secret "
                                                           "answer can't reach it without being stored. Give it to "
                                                           "the agent another way.", status=409)
            rule = self._make_rule(w, item, p) if p.rule else None
            decision = {**p.record, "rule_id": rule["id"] if rule else None}
            if w.execute("UPDATE review_items SET state = 'decided', decision = json(?), decided_by = json(?), "
                         "decided_at = ?, delivery = ? WHERE id = ? AND state = 'open'",
                         (json.dumps(decision), json.dumps(DECIDED_BY), w.now, delivery, item["id"])).rowcount != 1:
                raise _settled(w, item["id"])
            seq = w.append("review.decided", {"id": item["id"], "kind": item["kind"], "decision": decision,
                                              "decided_by": DECIDED_BY, "delivery": delivery, "rule": rule},
                           **_ids(w, item["id"]))
            if p.denied and item["session_id"] and self._denied(
                    w, item["session_id"], item["chat_id"], item["thread_ts"], item["detail"]["input_sha256"]):
                paused.append(item["session_id"])
            if p.pause and self.conversation is not None:
                self.conversation.paused(w, item)
            self._closed_item(w, item["id"])
            return seq

        call = native.get("call") if native else None
        if p.call is not None and call:
            await link.deliver_call(engine, item["id"], p.call, link.CallRef(**call), record)
        elif p.usage_continue is not None:
            await link.deliver_usage(engine, item["detail"]["pool"], keep_going=p.usage_continue,
                                     record=lambda w: record(w, None))
        elif p.wake is not None and item["session_id"]:
            def answered(w: link.Writes) -> None:
                seq = record(w, "wake")
                option = p.record.get("option") or p.record["decision"]
                text = self.conversation.answered(w, item, p.wake, option) if self.conversation else p.wake
                w.wake(item["session_id"], link.Wake(id=f"review:{item['id']}:{seq}", text=text, priority="now",
                                                     reason="review-answered", origin="system"))
            await link.write(engine, answered)
        else:
            await link.write(engine, lambda w: record(w, None))
        for session_id in paused:
            await link.pause(engine, session_id, why=loops.STUCK, by="loop_breaker")
        if p.pause and item["session_id"]:
            await link.pause(engine, item["session_id"], why=PAUSED_BY_PERSON, by="person")

    def _make_rule(self, w: link.Writes, item: Mapping[str, Any], p: Plan) -> dict[str, Any]:
        rule = p.rule
        return rule_store.create(w, scope=rule["scope"], kind=rule["kind"], match=match_of(rule["match"]),
                                 effect="allow", sentence=p.sentence, tier=p.tier,
                                 project_pid=rule["project_pid"], thread_ts=rule["thread_ts"], from_item=item["id"])

    # ── reads ────────────────────────────────────────────────────────────

    async def get_item(self, item_id: str) -> dict[str, Any]:
        row = await link.read(self._engine(), lambda conn: conn.execute(
            f"SELECT {COLUMNS} FROM review_items WHERE id = ?", (item_id,)).fetchone())
        if row is None:
            raise _not_found(item_id)
        return view(row)

    async def list_items(self, *, state: str | None = None, kind: str | None = None, chat: str | None = None,
                         thread: str | None = None, session: str | None = None, after_seq: int = 0,
                         before_seq: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Paged by seq. Open items come in the queue's order (destructive
        first, then blocking, then by age); nothing is hidden. ``after_seq``
        is the last item of the previous page."""
        queue = state == "open"

        def read(conn) -> list[tuple]:
            where = ["(:state IS NULL OR state = :state)", "(:kind IS NULL OR kind = :kind)",
                     "(:chat IS NULL OR chat_id = :chat)", "(:thread IS NULL OR thread_ts = :thread)",
                     "(:session IS NULL OR session_id = :session)", "(:before IS NULL OR seq < :before)"]
            params = {"state": state, "kind": kind, "chat": chat, "thread": thread, "session": session,
                      "before": before_seq, "after": after_seq, "limit": limit}
            if queue and after_seq:
                last = conn.execute("SELECT urgency FROM review_items WHERE seq = ?", (after_seq,)).fetchone()
                if last is None:
                    raise ReviewRefused("bad_cursor", "after_seq must be the seq of the last item of the previous "
                                                      "page.")
                where.append("(urgency, seq) > (:urgency, :after)")
                params["urgency"] = last[0]
            elif after_seq:
                where.append("seq > :after")
            order = "urgency, seq" if queue else "seq"
            return conn.execute(f"SELECT {COLUMNS} FROM review_items WHERE {' AND '.join(where)} "
                                f"ORDER BY {order} LIMIT :limit", params).fetchall()
        return [view(r) for r in await link.read(self._engine(), read)]

    # ── rules (the person's, from settings) ──────────────────────────────

    async def rules(self, *, after_seq: int = 0, limit: int = 100) -> dict[str, Any]:
        def read(conn) -> tuple:
            return rule_store.list_rules(conn, after_seq=after_seq, limit=limit), rule_store.default_views(conn)
        rules, defaults = await link.read(self._engine(), read)
        return {"rules": rules, "defaults": list(defaults.values()),
                "next_after_seq": rules[-1]["seq"] if len(rules) == limit else None}

    async def make_rule(self, *, scope: str, kind: str, match: Match, effect: str, sentence: str,
                        project_pid: str | None, thread_ts: str | None, client_id: str) -> dict[str, Any]:
        return await link.write(self._engine(), lambda w: rule_store.create_from_settings(
            w, scope=scope, kind=kind, match=match, effect=effect, sentence=sentence, project_pid=project_pid,
            thread_ts=thread_ts, client_id=client_id))

    async def revoke_rule(self, rule_id: str) -> dict[str, Any]:
        return await link.write(self._engine(), lambda w: rule_store.revoke(w, rule_id))

    # ── what the engine records (``Listener``, on the event loop) ────────

    def committed(self, events: Sequence[Any]) -> None:
        for event in events:
            if event.type in ("review.opened", "review.parked") and event.data.get("expires_at"):
                self._arm(event.data["expires_at"])
        for step in link.permission_steps(events):
            self._heard(step)

    def ephemeral(self, event: Any, epoch: int) -> None:
        step = link.permission_step(event)
        if step is not None:
            self._heard(step)

    def _heard(self, step: link.Asked | link.Resolved) -> None:
        key = (step.session_id, step.request_id)
        if isinstance(step, link.Asked):
            self._asked[key] = step.action_sha256
            while len(self._asked) > ASKED_REMEMBERED:
                self._asked.popitem(last=False)
            return
        action = self._asked.pop(key, None)
        if not step.allow and action is not None and not self._closed:
            self._spawn(self._policy_denial(step.session_id, step.request_id, action))

    async def _policy_denial(self, session_id: str, request_id: str, action: str) -> None:
        """A denial without an item is the policy's own (a person's deny is
        counted where it is recorded; a parked call's deny is no denial)."""
        def count(w: link.Writes) -> bool:
            if w.execute("SELECT 1 FROM review_items WHERE session_id = ? AND json_extract(native, '$.request_id') = ?",
                         (session_id, request_id)).fetchone():
                return False
            chat_id, thread_ts = w.execute("SELECT chat_id, thread_ts FROM sessions WHERE id = ?",
                                           (session_id,)).fetchone()
            return self._denied(w, session_id, chat_id, thread_ts, action)
        if await link.write(self._engine(), count):
            await link.pause(self._engine(), session_id, why=loops.STUCK, by="loop_breaker")

    def _denied(self, w: link.Writes, session_id: str, chat_id: str | None, thread_ts: str | None,
                action: str) -> bool:
        """Count a denial; open the suggestion when it is due. Returns
        whether the session is now stuck (to pause)."""
        scope = loops.scope_of(session_id, chat_id, thread_ts)
        outcome = loops.count(w, scope=scope, action=action)
        if outcome.suggest and not w.execute(
                "SELECT 1 FROM review_items WHERE kind = 'suggestion' AND state = 'open' AND "
                "json_extract(detail, '$.scope') = ?", (scope,)).fetchone():
            self.open_item(w, ItemSpec(kind="suggestion", title=loops.suggestion_title(bool(thread_ts),
                                                                                       outcome.denials),
                                       origin="xo", session_id=session_id, chat_id=chat_id, thread_ts=thread_ts,
                                       detail={"scope": scope, "denials": outcome.denials}))
        return outcome.pause

    # ── expiry ───────────────────────────────────────────────────────────

    def _arm(self, at: str | None) -> None:
        """Make sure the timer fires by ``at``."""
        if self._closed or at is None or self.engine is None or (self._due is not None and self._due <= at):
            return
        when = parse_ts(at)
        if when is None:
            return
        if self._timer is not None:
            self._timer.cancel()
        clock = link.clock(self.engine)
        self._due = at
        self._timer = clock.call_later(when.timestamp() - clock.time(), self._fire)

    def _fire(self) -> None:
        self._due, self._timer = None, None
        if not self._closed:
            self._spawn(self._expire_due())

    async def _expire_due(self) -> None:
        engine = self._engine()
        try:
            async with self._deciding:
                next_due = await link.write(engine, self._expire)
        except Exception as exc:  # noqa: BLE001 - the timer must outlive a failed write
            self._expiry_retry = min(EXPIRY_RETRY_MAX_S, self._expiry_retry * 2 if self._expiry_retry else
                                     EXPIRY_RETRY_FIRST_S)
            log.error("storm review: expiring items failed (%s); retrying in %s s",
                      getattr(exc, "message", exc), self._expiry_retry)
            clock = link.clock(engine)
            self._arm(link.iso(clock.time() + self._expiry_retry))
            return
        self._expiry_retry = 0.0
        self._arm(next_due)

    def _expire(self, w: link.Writes) -> str | None:
        due = w.execute("SELECT id, kind, title, session_id FROM review_items WHERE state = 'open' AND "
                        "expires_at IS NOT NULL AND expires_at <= ?", (w.now,)).fetchall()
        for item_id, kind, title, session_id in due:
            w.execute("UPDATE review_items SET state = 'expired' WHERE id = ?", (item_id,))
            w.append("review.expired", {"id": item_id, "kind": kind}, **_ids(w, item_id))
            if session_id:
                w.wake(session_id, link.Wake(id=f"review-expired:{item_id}", priority="later",
                                             reason="review-expired", origin="system",
                                             text=f"The person didn't answer within {EXPIRY.days} days, so this "
                                                  f"request expired: {title}"))
            self._closed_item(w, item_id)
        return _next_expiry(w)

    # ── plumbing ─────────────────────────────────────────────────────────

    def _closed_item(self, w: link.Writes, item_id: str) -> None:
        if self.conversation is not None:
            row = w.execute(f"SELECT {COLUMNS} FROM review_items WHERE id = ?", (item_id,)).fetchone()
            self.conversation.closed(w, view(row))

    def _engine(self) -> link.Engine:
        if self.engine is None:
            raise ReviewRefused("review_unavailable", "The review queue isn't running yet; retry shortly.", status=503)
        return self.engine

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("storm review: a background step failed", exc_info=task.exception())


async def boot(*, conversation: Conversation | None = None, **options: Any) -> link.Engine | None:
    """Start storm's engine with the review queue as its desk (the server's
    lifespan), with the conversation layer's part of each item. If the queue
    can't start, the engine is shut down again: an engine whose asks can't
    be decided must not run."""
    queue = ReviewQueue(conversation)
    engine = await link.boot(desk=queue, **options)
    if engine is None:
        return None
    try:
        await queue.start(engine)
    except BaseException:
        await queue.stop()
        await link.shutdown()
        raise
    return engine


async def shutdown() -> None:
    """Stop the queue's timer and tasks, then the engine (it parks what is held)."""
    try:
        desk = link.desk(link.current())
    except link.EngineUnavailable:
        return
    if isinstance(desk, ReviewQueue):
        await desk.stop()
    await link.shutdown()


def current() -> ReviewQueue:
    """The running engine's review queue (503 when this server runs none)."""
    desk = link.desk(link.current())
    if not isinstance(desk, ReviewQueue):
        raise ReviewRefused("review_unavailable", "This server's engine runs without the review queue.",
                            status=503)
    return desk


def _next_expiry(conn) -> str | None:
    return conn.execute("SELECT min(expires_at) FROM review_items WHERE state = 'open' AND "
                        "expires_at IS NOT NULL").fetchone()[0]


def _where(ask: link.Ask) -> dict[str, Any]:
    return {"session_id": ask.session_id, "chat_id": ask.chat_id, "thread_ts": ask.thread_ts, "run_id": ask.run,
            "turn_id": ask.turn_id, "epoch": ask.epoch}


def _ids(w: link.Writes, item_id: str) -> dict[str, Any]:
    chat_id, session_id, thread_ts, turn_id, run_id, epoch = w.execute(
        "SELECT chat_id, session_id, thread_ts, turn_id, run_id, epoch FROM review_items WHERE id = ?",
        (item_id,)).fetchone()
    return {"chat_id": chat_id, "session_id": session_id, "thread_ts": thread_ts, "turn_id": turn_id,
            "run_id": run_id, "epoch": epoch}


def _run(ask: link.Ask) -> dict[str, Any] | None:
    """The sub-agent that asked, by its title (ch 14 §6)."""
    return None if ask.run is None else {"id": ask.run, "title": ask.run_title}


def _title(summary: str, run: Mapping[str, Any] | None, verb: str) -> str:
    """"Run `npm test`", or "A sub-agent ('Check migrations') wants to run `npm test`"."""
    if run is None:
        return summary[:1].upper() + summary[1:]
    who = f"A sub-agent ('{run['title']}')" if run.get("title") else "A sub-agent"
    return f"{who} {verb} {summary}"


def _settled(w: link.Writes, item_id: str) -> Exception:
    """Why a decision found its item no longer open: it was decided (or is
    being decided) meanwhile, the agent withdrew it, or it expired."""
    state = w.execute("SELECT state FROM review_items WHERE id = ?", (item_id,)).fetchone()[0]
    if state == "withdrawn":
        return ReviewRefused("withdrawn", "The agent took this request back; there is nothing to decide.", status=409)
    if state == "expired":
        return ReviewRefused("expired", f"{item_id} expired unanswered; the agent was told.", status=410)
    return link.AlreadyDecided(item_id)


def _not_found(item_id: str) -> ReviewRefused:
    return ReviewRefused("review_not_found", f"There is no review item {item_id!r}. List them with "
                                             "GET /api/chat/review.", status=404)
