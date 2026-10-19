"""A thread's status card, rule-based (design ch 11 §7.3; the PoC's rules,
D58, with the UI audit's two fixes).

After each input of a thread, from its checklist (``status_md``: ✱ in
progress, ○ to do, ✓ done) and its replies in that input:

* the input failed → ``failed``;
* it replied and nothing is left (no ✱, no ○) → ``done``;
* it replied and left items to do, none in progress → ``needs_reply``;
* otherwise → ``working``.

``needs_you`` is the title of the thread's oldest open review item, else its
last reply when it needs a reply (audit P1-5); ``happened`` is the last
reply in full (audit P3-2). The thread's own state follows the card, except
that a resolved or paused thread stays as the person (or the agent) left it,
and one with an open blocking item stays ``needs_approval``.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

IN_PROGRESS, TO_DO = "✱", "○"
#: States a card never overrides.
KEPT = ("resolved", "paused")


def card(*, thread: Mapping[str, Any], replies: Sequence[str], failed: bool, error: str | None,
         oldest_open_item: str | None) -> dict[str, Any]:
    checklist = thread.get("status_md") or ""
    in_progress, to_do = IN_PROGRESS in checklist, TO_DO in checklist
    if failed:
        state = "failed"
    elif replies and not in_progress and not to_do:
        state = "done"
    elif replies and to_do and not in_progress:
        state = "needs_reply"
    else:
        state = "working"
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
