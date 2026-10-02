"""Custody session telemetry: cost and usage from the hash-chained ledger.

Every custody ledger entry carries measured token counts and a ``cost_usd``
derived from token accounting rather than from anything the agent declared -
under-declaring spend is one of the cheats its auditor rejects. That makes
this the rare telemetry source whose cost column is backed by a
tamper-evident record: rewriting the numbers breaks the hash chain.

Aggregated by
``services.cowork_agent.visualizer.session_telemetry.build_session_telemetry``
over every provider module named ``session_telemetry.py``. A session here is
one custody run: the entries from a ``run.started`` up to the next one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Optional

from services.cowork_agent.adapters.custody.visualizer_source import iter_ledgers

SOURCE_ID = "custody"
SOURCE_LABEL = "Custody"
META_PRIORITY = 10
COST_STATUS = "estimated"

SOURCE_CONFIG = {
    "vendor": "custody",
    "path_kind": "glob",
    "path_label": "Audit ledgers",
    "path_default": "~/xo-projects/**/.custody/ledger.jsonl",
    "collects": ["sessions", "tokens", "cost", "tools"],
}

_SESSION_ID_CHARS = 12


def _read_entries(ledger: Path) -> Iterator[dict[str, Any]]:
    """Yield the ledger's entries, skipping lines that do not parse.

    The viewer never repairs or judges the record; a malformed line is for
    ``custody verify`` to charge, not for telemetry to guess around.
    """
    try:
        text = ledger.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def _split_runs(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group a ledger's entries into runs at each ``run.started``."""
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("action") == "run.started" and current:
            runs.append(current)
            current = []
        current.append(entry)
    if current:
        runs.append(current)
    return runs


def _duration_seconds(first_ts: str, last_ts: str) -> Optional[int]:
    """Best-effort seconds between two ISO timestamps."""
    from datetime import datetime

    try:
        start = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        end = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = int((end - start).total_seconds())
    return max(seconds, 0)


def _session_row(
    project_id: str, ledger: Path, run: list[dict[str, Any]]
) -> dict[str, Any]:
    """Shape one run as a telemetry session row."""
    first, last = run[0], run[-1]
    seal = str(first.get("entry_hash", "")) or str(first.get("prev_hash", ""))
    session_id = seal[:_SESSION_ID_CHARS] or "run"
    tokens_in = sum(int(e.get("tokens_in") or 0) for e in run)
    tokens_out = sum(int(e.get("tokens_out") or 0) for e in run)
    cost = round(sum(float(e.get("cost_usd") or 0.0) for e in run), 6)
    tools: dict[str, int] = {}
    for entry in run:
        action = str(entry.get("action", ""))
        if action:
            tools[action] = tools.get(action, 0) + 1
    return {
        "id": session_id,
        "key": f"{SOURCE_ID}:{session_id}",
        "agent": SOURCE_ID,
        "project": project_id,
        "project_path": str(ledger.parent.parent),
        "started_at": str(first.get("ts", "")),
        "ended_at": str(last.get("ts", "")),
        "duration_sec": _duration_seconds(str(first.get("ts", "")), str(last.get("ts", ""))),
        "model": None,
        "agent_version": None,
        "turns": len(run),
        "fresh": tokens_in,
        "output": tokens_out,
        "cache_read": 0,
        "cache_write": 0,
        "tokens": tokens_in + tokens_out,
        "own_tokens": tokens_in + tokens_out,
        "total_tokens": tokens_in + tokens_out,
        "unclassified": 0,
        "breakdown_known": True,
        "cost": cost,
        "cost_known": True,
        "tools": [
            {"name": name, "calls": calls, "errors": 0}
            for name, calls in sorted(tools.items())
        ],
        "subagents": [],
    }


def _daily_rows(sessions: list[dict[str, Any]]) -> tuple[list[dict], list[dict], list[dict]]:
    """Aggregate per-day model, session and tool rows from session rows."""
    daily_sessions: list[dict[str, Any]] = []
    tools_by_day: dict[tuple[str, str], dict[str, int]] = {}
    for row in sessions:
        day = str(row["started_at"])[:10]
        daily_sessions.append({
            "day": day,
            "agent": SOURCE_ID,
            "session_id": row["id"],
            "session_key": row["key"],
            "tokens": row["tokens"],
            "unclassified": 0,
            "breakdown_known": True,
            "cost": row["cost"],
            "cost_known": True,
        })
        for tool in row["tools"]:
            bucket = tools_by_day.setdefault((day, tool["name"]), {"calls": 0, "errors": 0})
            bucket["calls"] += tool["calls"]
    daily_tools = [
        {"day": day, "agent": SOURCE_ID, "name": name,
         "calls": counts["calls"], "errors": counts["errors"]}
        for (day, name), counts in sorted(tools_by_day.items())
    ]
    return [], daily_sessions, daily_tools


def collect_session_telemetry() -> dict[str, Any]:
    """Return the telemetry payload for every custody run on disk.

    Raises:
        Exception: With a human-readable path when no ledger exists yet,
            which the Sources card surfaces as the reason.
    """
    sessions: list[dict[str, Any]] = []
    project_keys: set[str] = set()
    for project_id, ledger in iter_ledgers():
        entries = list(_read_entries(ledger))
        if not entries:
            continue
        project_keys.add(project_id)
        for run in _split_runs(entries):
            sessions.append(_session_row(project_id, ledger, run))
    if not sessions:
        raise Exception(
            "no custody ledger found under the projects root; run "
            "'custody harden <repo>' once to create <repo>/.custody/ledger.jsonl"
        )
    sessions.sort(key=lambda row: str(row["started_at"]), reverse=True)
    daily_models, daily_sessions, daily_tools = _daily_rows(sessions)
    return {
        "source": {"id": SOURCE_ID, "label": SOURCE_LABEL, "cost_status": COST_STATUS},
        "meta_priority": META_PRIORITY,
        "meta": {},
        "totals": {
            "sessions": len(sessions),
            "tokens": sum(int(row["tokens"]) for row in sessions),
            "cost_usd": round(sum(float(row["cost"]) for row in sessions), 6),
            "projects": len(project_keys),
            "sessions_by_agent": {SOURCE_ID: len(sessions)},
        },
        "project_keys": sorted(project_keys),
        "sessions": sessions,
        "daily_models": daily_models,
        "daily_sessions": daily_sessions,
        "daily_tools": daily_tools,
    }
