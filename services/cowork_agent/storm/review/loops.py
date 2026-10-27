"""The loop breaker (design ch 15 §9; decisions §0.64): it stops a loop of
asks, never parallelism or spend.

* **The same denied action 3 times in a row** pauses the thread (or the
  session, outside a thread): "stuck on a denied step". The person is told
  once, by the pause itself.
* **Every 100 denials in one thread** the person gets a suggestion item: pause
  it and have the coordinator start a fresh one (confirmed by holding the
  button, the UI's part). Dismissing it changes nothing.

A denial is the policy's deny, or the person's deny of a permission item; a
parked call's deny (the call waits for the person) is not one. "The same
action" is the policy's canonical action hash (``request_sha256``: the one a
grant, the stored permission step and the item bind), never the CLI's raw
input, which carries free text the agent rewrites on every retry.
"Paused" is a thread state (no new turns until the person resumes it), not
the parking of a call.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Identical denials in a row that pause the thread.
STREAK_TO_PAUSE = 3
#: Every this many denials in a thread, a suggestion to start fresh.
SUGGEST_EVERY = 100
STUCK = "stuck on a denied step"


@dataclass(frozen=True)
class Outcome:
    pause: bool = False
    suggest: bool = False
    denials: int = 0


def scope_of(session_id: str, chat_id: str | None, thread_ts: str | None) -> str:
    return f"thread:{chat_id}:{thread_ts}" if thread_ts else f"session:{session_id}"


def count(w, *, scope: str, action: str) -> Outcome:
    """Count one denial in ``scope`` (inside the transaction that records
    it). A pause starts a new streak, so after the person resumes, the same
    step pauses again only after three more denials."""
    row = w.execute("SELECT action, streak, denials FROM loop_breaker WHERE scope = ?", (scope,)).fetchone()
    streak = row[1] + 1 if row and row[0] == action else 1
    denials = (row[2] if row else 0) + 1
    pause = streak >= STREAK_TO_PAUSE
    w.execute("INSERT INTO loop_breaker (scope, action, streak, denials, updated_at) VALUES (?, ?, ?, ?, ?) "
              "ON CONFLICT (scope) DO UPDATE SET action = excluded.action, streak = excluded.streak, "
              "denials = excluded.denials, updated_at = excluded.updated_at",
              (scope, action, 0 if pause else streak, denials, w.now))
    return Outcome(pause=pause, suggest=denials % SUGGEST_EVERY == 0, denials=denials)


def suggestion_title(thread: bool, denials: int) -> str:
    if thread:
        return (f"This thread has had {denials} denials. If it isn't working well, pause it and ask the "
                "coordinator to start a fresh one.")
    return f"This session has had {denials} denials. If it isn't working well, pause it."
