"""Publish one row into the per-project session index after each hermes
streaming exchange.

Mirrors what ``adapters/claude_code/adapter.py:write_preliminary_entry``
and ``adapters/openclaw/transcript.py:tee_exchange`` do for their
backends. Without this, hermes sessions are invisible to the
per-project xo-coworker dashboard — the watcher never sees them
because hermes writes its real session state to SQLite, not JSONL.

Two things this module used to get wrong, both fixed by routing through
``engine.sessions_io`` (syncplan T19):

* it built ``xo_projects_root()/<agent_id>/.xo/sessions/`` by hand, bypassing
  ``resolve_project_dirname`` — so a differently-cased id wrote into a folder
  discovery never found, and ``mkdir(parents=True)`` conjured a ghost project
  next to the real one;
* it rewrote the WHOLE index document, so a concurrent write from another
  stream in the same project lost a row. The index is partitioned now: this
  writer replaces exactly its own row's shard.

V1 limitation: ``usage`` is seeded as zeros on the first turn and
carried forward untouched thereafter. Hermes records token
counts in ``~/.hermes/state.db`` / ``~/.hermes/profiles/<name>/state.db``
on a 3–10 s delay (see :func:`state_db.register_inflight_exchange`).
A future enhancement can backfill the usage block from state.db once
hermes commits; each subsequent turn merges a fresh ``updatedAt`` into
the existing row (fields this writer does not own — including a usage
block someone else backfilled — are preserved), so the totals will
catch up naturally once a reader exists. The row's per-turn refresh is
what matters for v1; the dashboard's "totalMessages" / "totalTokens"
widgets degrade gracefully to zero until the backfill lands.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from services.cowork_agent.engine import sessions_io as _session_index
from services.cowork_agent.project_layout import project_dir as _xo_project_dir

logger = logging.getLogger(__name__)


def _composite_key(agent_id: str, our_session_id: str) -> str:
    """``hermes:<agent_id>:<surface>:<8hex>`` — mirrors the openclaw
    shape parsed by the visualizer (sessions_io / source).

    The 8-hex suffix is derived from ``our_session_id`` so the same
    xo-cowork session always maps to the same composite key (and so
    repeated writes overwrite the same row instead of accumulating
    duplicates).
    """
    suffix = our_session_id.replace("-", "")[:8] if our_session_id else "00000000"
    return f"hermes:{agent_id}:web:{suffix}"


def write_session_row(
    *,
    agent_id: Optional[str],
    our_session_id: Optional[str],
    native_session_id: Optional[str],
) -> None:
    """Upsert one row in the project's session index.

    No-op when ``agent_id`` is missing (agent-only chat with no
    project selected) or ``native_session_id`` is missing (hermes
    didn't surface one — usually means the request errored before any
    session was created). All on-disk errors are swallowed with a
    log: the dashboard write must never fail a chat.
    """
    if not agent_id or not native_session_id:
        return
    try:
        composite = _composite_key(agent_id, our_session_id or native_session_id)
        existing = _session_index.read_session_index(agent_id).get(composite) or {}

        # Merge onto the row that is already there instead of rebuilding it.
        # Unlike the other adapters' initial-row literals this runs on EVERY
        # turn (adapter.py:50,115 — unguarded), so a fresh literal would drop
        # every field this writer does not own — ``title``, ``directoryHistory``,
        # a usage block backfilled from state.db, anything a later schema adds.
        # Same ``dict(existing) + update`` shape as openclaw/transcript.py.
        entry = dict(existing)
        # ``usage`` is carried forward, not recomputed here (see the module
        # docstring); seed the zero block only when there is nothing to carry.
        if not entry.get("usage"):
            entry["usage"] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
        entry.update({
            "sessionId": our_session_id or native_session_id,
            "nativeSessionId": native_session_id,
            "directory": str(_xo_project_dir(agent_id)),
            "backend": "hermes",
            "updatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        })

        _session_index.write_session_row(agent_id, composite, entry)
    except Exception as exc:  # noqa: BLE001 — never fail a chat for a dashboard write
        logger.warning("hermes session index write failed: %s", exc)
