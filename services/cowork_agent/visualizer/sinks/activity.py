"""Machine-local live-presence snapshot sink, one file per project."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.cowork_agent import coder_identity
from services.cowork_agent.visualizer.atomic_write import ChangeGate

# ── Write-on-change baseline (docs/syncplan.md §10, T26) ─────────────────────
#: One entry per project; a project that is deleted leaves its entry behind.
_gate = ChangeGate(limit=4096)


def reset_caches() -> None:
    """Drop the write-on-change baseline."""
    _gate.reset()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ms_to_iso(ms: Any) -> Optional[str]:
    """Epoch-milliseconds → ISO-8601, or ``None`` when unknown."""
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
    """Total order over presence rows (T24)."""
    return (
        str(row.get("session_id", "")),
        json.dumps(row, sort_keys=True, ensure_ascii=False),
    )


def _resolve_user_id() -> str:
    """
    Same answer as :mod:`project_json` and ``workspace/space_json`` — all three
    now resolve through :mod:`services.cowork_agent.coder_identity`.
    """
    return coder_identity.resolve_user_id()


def apply(
    activity_path: Path,
    presence_rows: list[dict],
    *,
    model_by_session: dict[str, str],
    host: Optional[str] = None,
) -> bool:
    """Write the live-presence snapshot for one project."""
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
        # Keys are inserted in schema order; an unknown timestamp is omitted
        # (T24) rather than stamped with the current time.
        opened_at = _ms_to_iso(r.get("started_at_ms"))
        if opened_at is not None:
            row["opened_at"] = opened_at
        last_activity_at = _ms_to_iso(r.get("updated_at_ms"))
        if last_activity_at is not None:
            row["last_activity_at"] = last_activity_at
        if host:
            row["host"] = host
        open_sessions.append(row)

    # Stable row order — the source enumerates a directory, and readdir order
    # is not a contract (T24).
    open_sessions.sort(key=_row_sort_key)

    payload = {
        "schema": 1,
        # The only time-varying field left in this document, and the one the
        # volatile exclusion below covers (T26).
        "updated_at": _now_iso(),
        "open_sessions": open_sessions,
    }
    return _gate.publish(activity_path, payload)
