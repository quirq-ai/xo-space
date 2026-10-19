"""A thread's status card, rule-based (design ch 11 §7.3; the PoC's rules,
D58, with the UI audit's two fixes).

After each input of a thread, from its checklist (``status_md``: ✱ in
progress, ○ to do, ✓ done) and its replies in that input. The input has
ended, so nothing is in progress any more: an item still marked ✱ is one the
agent didn't tick, and counts as finished (ch 11 §6.3: never "working" while
idle).

* the input failed → ``failed``;
* its last turn was stopped → ``needs_reply`` (§7.3);
* it handed a question to its coordinator (a routing call), or didn't reply
  → ``working``: it waits on an answer (the PoC's ✱ rule, kept for this);
* it replied and left nothing to do (no ○) → ``done``;
* it replied and left items to do → ``needs_reply``.

Departure from the PoC (D58), recorded in PR 12: a ✱ item no longer keeps a
thread that replied at ``working`` (live, finished threads stayed
``working`` while idle); waiting on the coordinator does.

``needs_you`` is the title of the thread's oldest open review item, else its
last reply when it needs a reply (audit P1-5); ``happened`` is the last
reply in full (audit P3-2). The thread's own state follows the card, except
that a resolved or paused thread stays as the person (or the agent) left it,
and one with an open blocking item stays ``needs_approval``.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

TO_DO = "○"
#: States a card never overrides.
KEPT = ("resolved", "paused")


def card(*, thread: Mapping[str, Any], replies: Sequence[str], failed: bool, error: str | None,
         oldest_open_item: str | None, stopped: bool = False, waiting: bool = False) -> dict[str, Any]:
    to_do = TO_DO in (thread.get("status_md") or "")
    if failed:
        state = "failed"
    elif stopped:
        state = "needs_reply"
    elif waiting or not replies:
        state = "working"
    else:
        state = "needs_reply" if to_do else "done"
    happened = replies[-1] if replies else (error or "")
    needs_you = oldest_open_item or (replies[-1] if state == "needs_reply" and replies else "")
    return {"state": state, "headline": thread["title"], "happened": happened, "needs_you": needs_you}


def thread_state(current: str, card_state: str, *, blocked: bool) -> str:
    """The thread's state after an input."""
    if current in KEPT:
        return current
    if blocked:
        return "needs_approval"
    return card_state
