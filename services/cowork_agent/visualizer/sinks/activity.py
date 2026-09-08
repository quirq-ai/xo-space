"""Machine-local live-presence snapshot sink, one file per project.

Consumes presence rows from ``Source.poll_presence()`` rather than
events. Each tick the watcher passes the most recent presence rows
filtered to one project; this sink writes a snapshot to disk. Stale
rows are dropped by the source (PID-alive check); this sink trusts
its input.

Required fields per ``activity.schema.json``:

* ``session_id`` — runtime's native session id
* ``runtime``
* ``agent`` — model id (e.g. ``claude-opus-4-7``). Filled from the
  ``model_by_session`` map maintained by the watcher loop from
  ``UsageObserved`` events. If never observed (session hasn't
  emitted an assistant turn yet) the row is dropped — schema
  forbids empty.
* ``user_id``
* ``opened_at`` — ISO-8601 from ``started_at_ms``
* ``last_activity_at`` — ISO-8601 from ``updated_at_ms``

Optional: ``host``.

**Determinism (docs/syncplan.md §10, T24).** This file is written
``N`` times per tick and is the hottest path write-on-change (T26)
has to fix, so identical input must produce a byte-identical
document. Two things used to make that false:

1. ``_ms_to_iso(0)`` returned *now*, so a session whose runtime file
   lacks ``startedAt``/``updatedAt`` (the source defaults both to 0)
   got a fresh ``opened_at``/``last_activity_at`` every single tick.
   That churn sits **inside** ``open_sessions``, where a top-level
   ``volatile`` exclusion cannot see it — write-on-change would have
   rewritten the file forever and never known. An unknown timestamp
   is now reported as unknown: the field is omitted rather than
   invented.
2. Row order followed the source's unsorted directory listing, so a
   readdir reorder read as a content change. ``open_sessions`` is
   now sorted by ``session_id``.

The one remaining time-varying field is the top-level ``updated_at``,
which is exactly what the ``volatile=("updated_at",)`` exclusion of
``write_json_atomic_if_changed`` is for.

**Write-on-change (T26).** Because of the above, an idle tick now writes
nothing here at all: the payload is compared against the one this process
last wrote and the ``os.replace`` is skipped when only ``updated_at``
moved. That removes the ``N`` of the idle ``N + 5`` writes a second. It
also means the ``updated_at`` on disk goes stale while the watcher is
healthy — deliberately. Freshness of this file is no longer a liveness
signal; ``~/.quirq/watcher/heartbeat.json`` (T22) is, and it is still
written unconditionally every tick.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.cowork_agent import coder_identity
from services.cowork_agent.visualizer.atomic_write import write_json_atomic_if_changed

# ── Write-on-change baseline (docs/syncplan.md §10, T26) ─────────────────────
#
# The payload this process last wrote, per target path. Syncplan §3 is
# explicit that the comparison baseline for a *fully owned* document comes
# from memory, not from a re-read: this sink is called once per project per
# tick, so re-reading would add N JSON parses to save N writes — close to a
# wash. Nothing else writes these files, which is what makes an in-memory
# baseline sound (see :func:`write_json_atomic_if_changed`).
#
# Keyed by path rather than held as one global because ``QUIRQ_STATE_ROOT``
# is re-read on every call: a root switch (every test, and a repointed
# deployment) must never let one root's snapshot answer for another's file.
_previous: dict[str, dict] = {}

#: A project that is deleted leaves its entry behind. Rather than pay a
#: prune every tick, the map is dropped wholesale once it grows past this —
#: one extra rewrite per live project, once, and self-healing. A real
#: workspace is three orders of magnitude below the cap.
_PREVIOUS_MAX = 4096


def reset_caches() -> None:
    """Drop the write-on-change baseline.

    For tests, and for a process whose state root moves under it. Losing
    the baseline costs one rewrite per project, never correctness.
    """
    _previous.clear()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_to_iso(ms: Any) -> Optional[str]:
    """Epoch-milliseconds → ISO-8601, or ``None`` when unknown.

    ``None`` (and the caller omitting the field) is the deliberate
    answer for a missing, zero, negative or unparseable value: the
    previous fallback to :func:`_now_iso` invented a timestamp that
    changed on every tick (see the module docstring, T24). Out-of-range
    values are also ``None`` rather than an exception — one malformed
    runtime file must not cost a whole project its presence snapshot.
    """
    try:
        value = int(ms or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    try:
        return (
            datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except (OSError, OverflowError, ValueError):
        return None


def _row_sort_key(row: dict) -> tuple[str, str]:
    """Total order over presence rows (T24).

    ``session_id`` is the meaningful key; the canonical JSON dump is a
    tiebreak so that even two rows sharing a session id (two runtime
    files for one resumed session) cannot leave the final order at the
    mercy of directory-listing order.
    """
    return (
        str(row.get("session_id", "")),
        json.dumps(row, sort_keys=True, ensure_ascii=False),
    )


def _resolve_user_id() -> str:
    """Same answer as :mod:`project_json` and ``workspace/space_json`` — all
    three now resolve through :mod:`services.cowork_agent.coder_identity`.

    Consistency is the point: a presence row saying ``user_id: "local"``
    beside a ``project.json`` saying ``owner_user_id: "ankitdwivedi"`` would
    describe two different people doing the same work.
    """
    return coder_identity.resolve_user_id()


def apply(
    activity_path: Path,
    presence_rows: list[dict],
    *,
    model_by_session: dict[str, str],
    host: Optional[str] = None,
) -> bool:
    """Write the live-presence snapshot for one project.

    ``activity_path`` is resolved by
    :func:`services.cowork_agent.visualizer.state.project_activity_path`;
    accepting the full path keeps this sink independent of the storage
    layout. Returns ``True`` if the file changed (or was created).

    ``presence_rows`` is the source's ``poll_presence()`` output
    pre-filtered to this project. ``model_by_session`` maps native
    session ids to the most recently observed model id; the sink
    drops rows whose model is unknown (the schema requires ``agent``).

    The emitted ``open_sessions`` list is sorted and carries no
    invented timestamps, so identical input yields an identical list —
    see the module docstring (T24).
    """
    user_id = _resolve_user_id()
    open_sessions: list[dict] = []

    for r in presence_rows:
        sid = r.get("session_id")
        if not sid:
            continue
        runtime = r.get("runtime")
        if not runtime:
            # Source MUST tag each presence row with its runtime. Dropping
            # here matches the "no session" treatment above — we'd rather
            # lose a presence row than mis-tag it as the wrong backend.
            continue
        agent = model_by_session.get(sid)
        if not agent:
            # Session live but no assistant message yet — invisible
            # state from the UI POV (docs/watcher-design.md §8.2).
            continue
        row = {
            "session_id":       sid,
            "runtime":          runtime,
            "agent":            agent,
            "user_id":          user_id,
        }
        # Keys are inserted in schema order; an unknown timestamp is
        # omitted (T24) rather than stamped with the current time.
        opened_at = _ms_to_iso(r.get("started_at_ms"))
        if opened_at is not None:
            row["opened_at"] = opened_at
        last_activity_at = _ms_to_iso(r.get("updated_at_ms"))
        if last_activity_at is not None:
            row["last_activity_at"] = last_activity_at
        if host:
            row["host"] = host
        open_sessions.append(row)

    # Stable row order — the source enumerates a directory, and readdir
    # order is not a contract (T24).
    open_sessions.sort(key=_row_sort_key)

    payload = {
        "schema": 1,
        # The only time-varying field left in this document, and the one
        # the volatile exclusion below covers (T26). It goes stale on disk
        # whenever nothing else moved; the heartbeat is the liveness signal.
        "updated_at": _now_iso(),
        "open_sessions": open_sessions,
    }
    key = str(activity_path)
    if key in _previous and activity_path.exists():
        # Steady state: one ``stat`` and a dict comparison, no read.
        changed = write_json_atomic_if_changed(
            activity_path, payload, ("updated_at",), previous=_previous[key]
        )
    else:
        # No baseline yet (first tick after a restart), or the file was
        # removed underneath us — ``rm -rf ~/.quirq`` is a documented clean
        # reset (syncplan §4), and a cached baseline alone would keep the
        # file missing until its content happened to change. Let the helper
        # take its baseline from disk: absent or corrupt writes, an
        # identical file after a restart does not.
        changed = write_json_atomic_if_changed(
            activity_path, payload, ("updated_at",)
        )
    if key not in _previous and len(_previous) >= _PREVIOUS_MAX:
        _previous.clear()
    _previous[key] = payload
    return changed
