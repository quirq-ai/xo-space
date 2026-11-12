"""The founder's Stop, pause and resume on a scope, what the founder's own
message lifts, and the founder's paid-usage mode (w2-w3 design §4; plan W2).
It speaks only the engine and the records, and names no agent.

* **Stop** is one transaction: the ``run=stopped`` intent
  (``records.intents.set_intent``, with its event and ledger entry), every
  queued wake of the scope's sessions dropped (``wake.dropped {ids, reason:
  "stopped"}``), and every open review item of theirs withdrawn
  (``review.withdrawn {reason: "stopped"}``). Once it has committed, every
  live session in the scope is halted (``Engine.halt``: the turn
  interrupted, the process released, every marked process stopped), all at
  once, and what the scope's other sessions left running (a background job
  of a session now idle) is stopped too, by its marker, with one scan of
  ``/proc`` (``sweep.sweep_all``, said with one ``scope.swept`` event). It
  returns when all of it has ended, with the times.
* **Pause** is the same, except that queued wakes stay queued and review
  items stay open. Running turns are halted too, because the founder acted:
  a turn the halt cut short leaves its session one held wake that says so,
  so the agent hears it was paused mid-turn when it resumes.
* **Resume** clears the intent, then admission runs: what was held arrives
  as one input (the engine folds a session's queued wakes together), the
  founder's messages word for word.
* **The founder's own message** (``own_message``), in the transaction that
  stores it, lifts a stop or pause on exactly the scope it was written to:
  its thread (and that thread's session), a plain chat's session, or the
  coordinator for a timeline message. A stop or pause on a broader scope
  (the chat, the Space) still holds, and the message waits with the rest
  until Resume.
* **Paid** (``set_paid``): ``never`` or ``ask`` for the Space or one pool,
  until a time or for good, with its event and ledger entry. The pools it
  governs are recomputed at once, and the live sessions of one the paid
  gate now holds are halted (``never`` on an account past its threshold).
  The founder's answer to a paid question (Continue, Wait) is not this: it
  is for that window only, and leaves the mode as it is.

While an intent holds, the engine refuses or holds every wake
(``Writes.wake``), skips the scope in admission, refuses new sessions there
(``Engine.create_session``) and the comms tools refuse its agents' calls
(``comms.calls``). Nothing here runs in normal work, and no agent can call
it: only the founder's routes (``routers/cowork_agent/storm/intent.py``)
and the founder's own message do.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from typing import Any

from services.cowork_agent.storm.control import sweep
from services.cowork_agent.storm.control.halt import Halted
from services.cowork_agent.storm.engine import Engine, Wake, Writes
from services.cowork_agent.storm.engine import records
from services.cowork_agent.storm.records import intents, ledger
from services.cowork_agent.storm.records.intents import Intent, Scope
from services.cowork_agent.storm.store import RESUME_NOTE, last_step, scrub

log = logging.getLogger(__name__)

#: Why a halted session's turn ended, in its pause note.
PAUSED_MID_TURN = "because the founder paused this work"
#: The sessions of each scope that takes a ``run`` intent (``retired_at`` is checked apart).
_IN_SCOPE = {"space": "1 = 1", "chat": "chat_id = :id", "coordinator": "chat_id = :id AND role LIKE '%coordinator'",
             "thread": "chat_id = :chat AND thread_ts = :ts", "session": "id = :id"}


@dataclass(frozen=True)
class Acted:
    """What one Stop or pause did, and how long it took to end the work."""

    intent: Intent
    sessions: tuple[str, ...]                # the scope's live (not retired) sessions
    dropped: tuple[str, ...]                 # queued wakes dropped (a Stop)
    withdrawn: tuple[str, ...]               # open review items withdrawn (a Stop)
    halted: tuple[Halted, ...]               # one per session that had a process
    swept: sweep.Swept                       # what the scope's other sessions left running
    total_s: float                           # from the act to the end of the last halt or sweep

    def record(self) -> dict[str, Any]:
        """What the founder's route answers."""
        return {"intent": dataclasses.asdict(self.intent), "sessions": list(self.sessions),
                "dropped": list(self.dropped), "withdrawn": list(self.withdrawn),
                "halted": [{"session_id": h.session_id, "turn_id": h.turn_id, "interrupted": h.interrupted,
                            "released": h.released, **h.swept.record(), "ms": _ms(h.total_s)} for h in self.halted],
                "swept": self.swept.record(), "ms": _ms(self.total_s)}


async def stop(engine: Engine, scope: str, scope_id: str, *, cites: str | None = None) -> Acted:
    """Stop all work in the scope until the founder resumes it."""
    return await _hold(engine, "stopped", scope, scope_id, cites)


async def pause(engine: Engine, scope: str, scope_id: str, *, cites: str | None = None) -> Acted:
    """Hold all work in the scope, keeping what is queued, until the founder resumes it."""
    return await _hold(engine, "paused", scope, scope_id, cites)


async def resume(engine: Engine, scope: str, scope_id: str) -> bool:
    """Lift the founder's stop or pause on the scope; whether there was one.
    Admission runs at once, so what was held there goes as one input per
    session (a broader scope's hold still holds)."""
    cleared = await engine.write(lambda w: intents.clear_intent(w, scope=scope, scope_id=scope_id, key="run"))
    engine.kick()
    return cleared


@dataclass(frozen=True)
class PaidSet:
    """What one paid-mode change did: the intent, and the halts it caused."""

    intent: Intent
    halted: tuple[Halted, ...]

    def record(self) -> dict[str, Any]:
        """What the founder's route answers."""
        return {"intent": dataclasses.asdict(self.intent),
                "halted": [{"session_id": h.session_id, "turn_id": h.turn_id, "interrupted": h.interrupted,
                            "released": h.released, "ms": _ms(h.total_s)} for h in self.halted]}


async def set_paid(engine: Engine, scope: str, scope_id: str, value: str, *, until: str | None = None,
                   cites: str | None = None) -> PaidSet:
    """Set the founder's paid mode (``never`` or ``ask``) for the Space or a
    pool, until ``until`` or for good. Then every pool it governs is
    recomputed, and the live sessions of each the paid gate now holds are
    halted before this returns."""
    intent = await engine.write(lambda w: intents.set_intent(w, scope=scope, scope_id=scope_id, key="paid",
                                                             value=value, until=until, cites=cites))
    halted = await engine.refresh_pools([scope_id] if scope == "pool" else None)
    log.info("storm: the founder set paid=%s on %s %r%s; %d session(s) halted", value, scope, scope_id,
             f" until {intent.until}" if intent.until else "", len(halted))
    return PaidSet(intent, tuple(halted))


def own_message(w: Writes, *scopes: Scope) -> None:
    """In the transaction storing the founder's own message: lift a stop or
    pause on exactly ``scopes``, the ones the message was written to."""
    for scope, scope_id in scopes:
        intents.clear_intent(w, scope=scope, scope_id=scope_id, key="run")


async def live(engine: Engine) -> list[Intent]:
    """Every intent that holds now, ``run`` and ``paid``."""
    now = engine.clock.time()
    found = await engine.store.read(intents.load)
    return [intent for _key, intent in sorted(found.items()) if intent.live(now)]


async def _hold(engine: Engine, value: str, scope: str, scope_id: str, cites: str | None) -> Acted:
    loop = asyncio.get_running_loop()
    started = loop.time()

    def hold(w: Writes) -> tuple[Intent, list[str], list[str], list[str]]:
        intent = intents.set_intent(w, scope=scope, scope_id=scope_id, key="run", value=value, cites=cites)
        sessions = _sessions(w, scope, scope_id)
        dropped: list[str] = []
        withdrawn: list[str] = []
        if value == "stopped":
            for session_id, status, epoch, chat_id, thread_ts in sessions:
                ids = records.Ids(session_id, chat_id, thread_ts)
                gone = [r[0] for r in w.execute("UPDATE wakes SET state = 'dropped', done_at = ? WHERE session_id = ? "
                                                "AND state = 'queued' RETURNING id", (w.now, session_id)).fetchall()]
                if gone:
                    scrub(w.tx, gone)
                    w.append("wake.dropped", {"ids": gone, "reason": "stopped"}, **ids.columns(epoch=epoch))
                    dropped += gone
                if status == "queued":          # it waited to run those: it is cold now
                    records.set_status(w, ids, "cold", epoch, {"reason": "stopped"})
                for (item,) in w.execute("SELECT id FROM review_items WHERE session_id = ? AND state = 'open' "
                                         "ORDER BY seq", (session_id,)).fetchall():
                    engine.desk.withdraw(w, item, "stopped")
                    withdrawn.append(item)
        return intent, [s[0] for s in sessions], dropped, withdrawn
    intent, sessions, dropped, withdrawn = await engine.write(hold)
    active = engine.active(sessions)
    halted, swept = await asyncio.gather(_halt(engine, active, value),
                                         sweep.sweep_all(engine.space, (s for s in sessions if s not in active)))
    if swept.found:
        await engine.write(lambda w: w.append("scope.swept", {"scope": scope, "scope_id": scope_id, "reason": value,
                                                              **swept.record()}))
    if value == "paused":
        await _note_paused(engine, [h for h in halted if h.interrupted and h.turn_id])
    acted = Acted(intent=intent, sessions=tuple(sessions), dropped=tuple(dropped), withdrawn=tuple(withdrawn),
                  halted=tuple(halted), swept=swept, total_s=loop.time() - started)
    log.info("storm: the founder %s %s %r: %d session(s), %d halted and %d leftover process(es) stopped in %.0f ms, "
             "%d wake(s) dropped, %d item(s) withdrawn", value, scope, scope_id, len(sessions), len(halted),
             len(swept.killed), acted.total_s * 1000, len(dropped), len(withdrawn))
    return acted


def _sessions(w: Writes, scope: str, scope_id: str) -> list[tuple[str, str, int, str | None, str | None]]:
    """The scope's sessions that aren't retired: (id, status, epoch, chat, thread)."""
    chat, _, ts = scope_id.partition("/")
    return w.execute(f"SELECT id, status, epoch, chat_id, thread_ts FROM sessions WHERE retired_at IS NULL AND "
                     f"{_IN_SCOPE[scope]} ORDER BY seq", {"id": scope_id, "chat": chat, "ts": ts}).fetchall()


async def _halt(engine: Engine, session_ids: list[str], why: str) -> list[Halted]:
    """Halt each session at once; every halt has ended when this returns.
    One that fails is logged, and the others' results still come back."""
    done = await asyncio.gather(*(engine.halt(s, reason=why, by=ledger.FOUNDER) for s in session_ids),
                                return_exceptions=True)
    halted = []
    for session_id, result in zip(session_ids, done):
        if isinstance(result, BaseException):
            log.error("storm: halting %s for the founder failed: %r", session_id, result)
        else:
            halted.append(result)
    return halted


async def _note_paused(engine: Engine, halted: list[Halted]) -> None:
    """A turn the pause cut short: its input was the turn's (the CLI keeps
    it), so its session gets one held wake saying where it stopped, which
    arrives with the rest on resume."""
    if not halted:
        return

    def note(w: Writes) -> None:
        for h in halted:
            text = RESUME_NOTE.format(step=last_step(w.tx.conn, h.turn_id), why=PAUSED_MID_TURN)
            w.wake(h.session_id, Wake(id=f"paused:{h.turn_id}", text=text, reason="paused", origin="system"))
    await engine.write(note)


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)
