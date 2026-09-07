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
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.cowork_agent.visualizer.atomic_write import write_json_atomic


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_to_iso(ms: int) -> str:
    if not ms:
        return _now_iso()
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _resolve_user_id() -> str:
    """Same lookup as :mod:`project_json`. Per docs/watcher-design.md
    §8.1: ``get_auth_state().get("user_id") or "local"``."""
    try:
        from routers.auth.auth import get_auth_state
        return get_auth_state().get("user_id") or "local"
    except Exception:
        return "local"


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
            "opened_at":        _ms_to_iso(int(r.get("started_at_ms", 0) or 0)),
            "last_activity_at": _ms_to_iso(int(r.get("updated_at_ms", 0) or 0)),
        }
        if host:
            row["host"] = host
        open_sessions.append(row)

    payload = {
        "schema": 1,
        "updated_at": _now_iso(),
        "open_sessions": open_sessions,
    }
    write_json_atomic(activity_path, payload)
    # Always claim "changed" — the sink is idempotent and writing
    # the same snapshot is cheap. (If we tracked equality we'd save
    # one fsync per tick when nothing changed; not worth the
    # complexity for the activity file.)
    return True
