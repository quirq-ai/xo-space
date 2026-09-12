"""Environment-derived runtime facts that are needed below the services layer.

Two things both ``services/`` and ``utils/`` have to agree on, defined once:

- ``quirq_state_dir()`` — the machine-local Quirq state root. Re-exported by
  ``services.cowork_agent.local_state`` (the documented entry point for
  service code); ``utils/commands/scheduler.py`` imports it from here because
  ``utils/`` must not import ``services/``.
- ``watcher_tick_interval_seconds()`` — how often the visualizer watcher
  ticks. The watcher uses it to sleep; the command scheduler uses it as the
  only lower bound on a job's interval (it is called once per tick, so a
  shorter interval could not be honoured).

No other module reads these two environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_STATE_ROOT = "QUIRQ_STATE_ROOT"
ENV_WATCHER_INTERVAL = "QUIRQ_WATCHER_INTERVAL_SECONDS"


def quirq_state_dir() -> Path:
    """``~/.quirq/`` or ``$QUIRQ_STATE_ROOT``."""
    configured = (os.getenv(ENV_STATE_ROOT, "") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".quirq"


def watcher_tick_interval_seconds() -> float:
    """Seconds between watcher ticks: ``$QUIRQ_WATCHER_INTERVAL_SECONDS``,
    default 1, clamped to 0.25–60 (below that the tick would spin; above it the
    liveness view would show the watcher as dead between ticks)."""
    raw = (os.getenv(ENV_WATCHER_INTERVAL, "1") or "1").strip()
    try:
        interval = float(raw)
    except ValueError:
        interval = 1.0
    return min(60.0, max(0.25, interval))
