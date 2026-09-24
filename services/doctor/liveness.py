"""Is each background part of the Space alive and succeeding? (#188 design §7)

Two layers. The in-process task record (``services/background.py``, read as
``ctx.components``) says whether a task crashed, when, and with what; it is
empty when the doctor runs outside the server. What each component leaves on
disk (heartbeat, poll records, mirrors, schedules) says whether a loop that
is still alive is stuck, and carries across restarts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from services.cowork_agent import runtime_config
from services.cowork_agent.visualizer.state import watcher_heartbeat_path
from services.doctor import inventory
from services.doctor.context import Context
from services.timestamps import parse_ts
from utils import runtime_env

WATCHER = "watcher"


def watcher_enabled() -> bool:
    """``QUIRQ_WATCHER_ENABLED`` (default true), parsed the same way
    ``runtime_config.effective_settings`` does, but without resolving the
    active agent: that also happens inside ``effective_settings`` and has
    nothing to do with these two watcher env vars, so a broken agent setup
    must not turn the watcher or layout check into ERROR."""
    as_bool = getattr(runtime_config, "_as_bool", None)
    if as_bool is None:  # pragma: no cover - defensive fallback only
        def as_bool(value: str | None, *, default: bool) -> bool:
            if value is None:
                return default
            return value.strip().lower() in {"1", "true", "yes", "on"}
    return as_bool(os.getenv("QUIRQ_WATCHER_ENABLED"), default=True)


def stale_after() -> float:
    # Borrowed inside the function: a rename upstream must turn one check
    # into an ERROR result, not stop the server importing the doctor router.
    from services.cowork_agent.quirq_catalog import _stale_after_seconds  # the Quirq view's liveness rule, shared

    return _stale_after_seconds(runtime_env.watcher_tick_interval_seconds())


def heartbeat_age(ctx: Context, path: Optional[Path] = None,
                  spec: Optional[inventory.Spec] = None) -> Optional[float]:
    """Seconds since ``last_tick_at`` in the heartbeat at ``path`` (default:
    the current one), or None when there is no readable stamp."""
    if path is None:
        path = watcher_heartbeat_path()
        spec = inventory.spec_for(inventory.STATE, "cache/heartbeat.json")
    value = ctx.read(path, spec).value
    stamp = parse_ts(value.get("last_tick_at")) if isinstance(value, dict) else None
    return None if stamp is None else max(0.0, ctx.now - stamp.timestamp())


def watcher_dead(ctx: Context) -> bool:
    """The task record says the watcher task ended (crashed or returned)."""
    record = ctx.components.get(WATCHER)
    return record is not None and record.get("state") in ("crashed", "returned")


def watcher_alive(ctx: Context) -> bool:
    """Enabled, not known dead, and its heartbeat is fresh."""
    if not watcher_enabled() or watcher_dead(ctx):
        return False
    age = heartbeat_age(ctx)
    return age is not None and age <= stale_after()


def usage_state_path(ctx: Context) -> Optional[Path]:
    """The usage-report bookmark the server actually reads: USAGE_SYNC_STATE_FILE,
    else ``usage/<active agent>.json`` (services/usage_sync.py:40-44). None
    when the active agent can't be resolved."""
    override = (os.getenv("USAGE_SYNC_STATE_FILE", "") or "").strip()
    try:
        if override:
            return Path(override).expanduser().resolve()
        from services.cowork_agent.registry.agent_registry import get_active_agent

        return ctx.state_root / "usage" / f"{get_active_agent().name}.json"
    except Exception:  # noqa: BLE001 - an unresolvable agent judges every file instead
        return None
