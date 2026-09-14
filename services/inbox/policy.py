"""Declarative loading policy for the inbox feeders and retention.

One table in one place, so the "what / when / how much loads" rules are
readable and testable instead of being magic numbers scattered across
:mod:`feeders`. For each source it declares:

* ``bootstrap`` -- how far back a feeder reaches on a cold start (no cursor
  yet). ``None`` means the feeder has no cold-start window of its own.
* ``fetch_limit`` -- how many rows the feeder reads per run. ``None`` means
  it reads everything the source hands it (a project's issue mirror, a
  sharing snapshot, every project's todos).
* ``future_slack`` -- a producer timestamp further ahead than this never
  pins the cursor, so one clock-skewed or forward-dated event cannot push
  the floor into the future and starve everything arriving now. ``None``
  means the feeder does not guard the cursor against future timestamps.
* ``retention_max`` -- keep at most this many items of this source in the
  file. ``None`` means the source is bounded only by the global cap
  (``store.MAX_ITEMS``). A per-source quota keeps one noisy feeder (a busy
  calendar or timeline) from crowding every other source out of the cap.

Content configuration a person can override per source -- ``enabled`` and
the type/status/state filters -- still lives in ``store.DEFAULT_SOURCES``
and merges through ``store.source_config``. This table is the operational
policy the code owns; it is deliberately not user-editable through the
inbox file.

Standalone by design: it imports nothing from the package, so both
:mod:`feeders` and :mod:`store` can read it without a cycle.
"""

from __future__ import annotations

from datetime import timedelta
from typing import NamedTuple, Optional


class SourcePolicy(NamedTuple):
    bootstrap: Optional[timedelta]
    fetch_limit: Optional[int]
    future_slack: Optional[timedelta]
    retention_max: Optional[int]


# Sources not listed fall back to this (also what an unknown item source
# retains under): a day's cold start, no fetch cap, no future guard, and
# only the global item cap for retention.
DEFAULT = SourcePolicy(bootstrap=timedelta(hours=24), fetch_limit=None,
                       future_slack=None, retention_max=None)

POLICY: dict[str, SourcePolicy] = {
    # newest-first workspace events; the fetch cap bounds one run, the quota
    # bounds how much of the file a chatty workspace may occupy.
    "timeline": SourcePolicy(bootstrap=timedelta(hours=24), fetch_limit=500,
                             future_slack=None, retention_max=200),
    # a full snapshot every run (ts = now), bounded by the watched set and
    # auto-close, so it needs no window, fetch cap, or quota.
    "todos": SourcePolicy(bootstrap=None, fetch_limit=None,
                          future_slack=None, retention_max=None),
    # relay transitions from the sharing snapshot's own recent list.
    "sharing": SourcePolicy(bootstrap=None, fetch_limit=None,
                            future_slack=None, retention_max=None),
    # issues move slower than the timeline, so a week is the first read; a
    # mirror updated_at more than a day ahead never pins the cursor.
    "issues": SourcePolicy(bootstrap=timedelta(days=7), fetch_limit=None,
                           future_slack=timedelta(days=1), retention_max=None),
    # per-toolkit events.jsonl; a calendar collector can stamp events up to a
    # week ahead, so only clock skew (5 min) is tolerated for the cursor, and
    # the quota keeps a busy toolkit from dominating the file.
    "connections": SourcePolicy(bootstrap=timedelta(hours=24), fetch_limit=200,
                                future_slack=timedelta(minutes=5), retention_max=200),
}


def policy(name: str) -> SourcePolicy:
    """The policy for ``name``, or :data:`DEFAULT` for an unknown source."""
    return POLICY.get(name, DEFAULT)
