"""Read-only Space session telemetry for local Hermes sessions.

Hermes rolls usage up per session in each profile's SQLite ``state.db``
(``~/.hermes/state.db`` for the default profile, ``profiles/<name>/state.db``
for the others): ``input_tokens`` without cache, ``output_tokens`` with
reasoning already inside it, ``cache_read_tokens``, ``cache_write_tokens``, and
a cost with its status (``actual``, ``estimated``, ``included``, ``unknown``).
This provider reads those columns and, for tool counts, only the ``role`` and
``tool_name`` of message rows. Prompt text, titles, tool arguments and output
are never selected.

Hermes records usage per session, not per turn, so a session's tokens are
counted on the day it started. Sessions started through the API server carry
no ``cwd``; their project comes from the per-project session index XO Space
writes for every chat (``~/.quirq/projects/<pid>/sessions/``).

Hermes is not installed when its home has no ``state.db``: the source is then
reported unavailable and contributes nothing.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from services.cowork_agent.adapters.hermes.paths import HERMES_DIR
from services.cowork_agent.adapters.hermes.state_db import _profile_state_dbs
from services.cowork_agent.engine import sessions_io

SOURCE_ID = "hermes"
SOURCE_LABEL = "Hermes"
META_PRIORITY = 5
COST_STATUS = "estimated"
SOURCE_CONFIG = {
    "vendor": "nous",
    "path_default": str(HERMES_DIR),
    "path_kind": "dir",
    "path_label": "Hermes home",
    "collects": ["Sessions and models", "Token usage", "Tool calls"],
    "never": "No prompt text stored",
}

MAX_SESSIONS = 500
MAX_TOOLS_PER_SESSION = 10
_BUSY_TIMEOUT_MS = 2000
_KNOWN_COST = {"actual", "estimated", "included"}
_OPTIONAL_COLUMNS = (
    "model", "ended_at", "last_activity_at", "cwd", "parent_session_id",
    "cache_read_tokens", "cache_write_tokens", "estimated_cost_usd",
    "actual_cost_usd", "cost_status",
)
_REQUIRED_COLUMNS = ("id", "started_at", "input_tokens", "output_tokens")


def runtime_mounts() -> list[Path]:
    """Native directories the managed Docker runtime may read if present."""
    return [HERMES_DIR]


def _connect_ro(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    connection.execute(f"pragma busy_timeout={_BUSY_TIMEOUT_MS}")
    return connection


def _iso(epoch: Any) -> str | None:
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return None
    moment = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _int(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _read_profile(
    profile: str, path: Path,
) -> tuple[list[dict], dict[str, dict[str, int]], dict[str, int]]:
    """Session rows, per-session tool counts and user turns from one profile's state.db."""
    connection = _connect_ro(path)
    try:
        columns = {str(row[1]) for row in connection.execute("pragma table_info(sessions)")}
        missing = [name for name in _REQUIRED_COLUMNS if name not in columns]
        if missing:
            raise ValueError(f"Hermes state DB {path} has no sessions columns {missing}")
        selected = [*_REQUIRED_COLUMNS, *(name for name in _OPTIONAL_COLUMNS if name in columns)]
        cursor = connection.execute(f"select {', '.join(selected)} from sessions")
        rows = [
            {**{name: None for name in _OPTIONAL_COLUMNS}, **dict(zip(selected, values)), "profile": profile}
            for values in cursor
        ]
        tools: dict[str, dict[str, int]] = defaultdict(dict)
        turns: dict[str, int] = {}
        message_columns = {str(row[1]) for row in connection.execute("pragma table_info(messages)")}
        if {"session_id", "role", "tool_name"} <= message_columns:
            for session_id, name, calls in connection.execute(
                "select session_id, tool_name, count(*) from messages "
                "where role = 'tool' and tool_name is not null group by session_id, tool_name"
            ):
                tools[str(session_id)][str(name)] = int(calls)
        if {"session_id", "role"} <= message_columns:
            for session_id, count in connection.execute(
                "select session_id, count(*) from messages where role = 'user' group by session_id"
            ):
                turns[str(session_id)] = int(count)
        return rows, tools, turns
    finally:
        connection.close()


def _index_directories() -> dict[str, str]:
    """Native session id → project directory, from this backend's index rows."""
    directories: dict[str, str] = {}
    for _project_id, project_dir, index in sessions_io.iter_project_session_indexes():
        for row in index.values():
            if row.get("backend") != SOURCE_ID:
                continue
            native = row.get("nativeSessionId") or row.get("sessionId")
            if native:
                directories[str(native)] = str(row.get("directory") or project_dir)
    return directories


def _cost(row: dict) -> tuple[float, bool]:
    actual, estimated = row.get("actual_cost_usd"), row.get("estimated_cost_usd")
    amount = actual if isinstance(actual, (int, float)) else estimated
    amount = float(amount) if isinstance(amount, (int, float)) and amount >= 0 else 0.0
    known = isinstance(actual, (int, float)) or row.get("cost_status") in _KNOWN_COST
    return amount, known


def collect_session_telemetry() -> dict:
    databases = _profile_state_dbs()
    if not databases:
        raise FileNotFoundError(f"Hermes state DB not found under {HERMES_DIR}")

    rows: list[dict] = []
    tools_by_session: dict[str, dict[str, int]] = {}
    turns_by_session: dict[str, int] = {}
    unreadable: list[str] = []
    for profile, path in databases:
        try:
            profile_rows, profile_tools, profile_turns = _read_profile(profile, path)
        except (OSError, sqlite3.Error, ValueError) as exc:
            unreadable.append(f"{path}: {exc}")
            continue
        rows.extend(profile_rows)
        tools_by_session.update(profile_tools)
        turns_by_session.update(profile_turns)
    if unreadable and not rows:
        raise ValueError(f"Hermes state DB could not be read ({'; '.join(unreadable)})")

    for row in rows:
        row["fresh"] = _int(row["input_tokens"])
        row["output"] = _int(row["output_tokens"])
        row["cache_read"] = _int(row["cache_read_tokens"])
        row["cache_write"] = _int(row["cache_write_tokens"])
        row["tokens"] = row["fresh"] + row["output"] + row["cache_read"] + row["cache_write"]

    by_id = {str(row["id"]): row for row in rows}
    children: dict[str, list[dict]] = defaultdict(list)
    roots: list[dict] = []
    for row in rows:
        parent = row.get("parent_session_id")
        if parent and str(parent) in by_id and str(parent) != str(row["id"]):
            children[str(parent)].append(row)
        else:
            roots.append(row)

    def tree(root: dict) -> list[dict]:
        out, stack, seen = [], [root], set()
        while stack:
            node = stack.pop()
            if str(node["id"]) in seen:
                continue
            seen.add(str(node["id"]))
            out.append(node)
            stack.extend(children.get(str(node["id"]), []))
        return out

    directories = _index_directories()
    meaningful = [root for root in roots if sum(node["tokens"] for node in tree(root)) > 0]
    meaningful.sort(key=lambda row: float(row["started_at"] or 0), reverse=True)

    # [tokens, cost, cost known for every contributing session]
    daily_models: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0.0, True])
    daily_sessions: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0.0, True])
    daily_tools: dict[tuple[str, str], int] = defaultdict(int)
    sessions: list[dict] = []
    projects: set[str] = set()
    total_tokens = 0
    total_cost = 0.0

    for index, root in enumerate(meaningful):
        nodes = tree(root)
        root_id = str(root["id"])
        day = (_iso(root["started_at"]) or "")[:10]
        for node in nodes:
            amount, known = _cost(node)
            total_tokens += node["tokens"]
            total_cost += amount
            if day:
                for values in (daily_models[(day, node.get("model") or "unknown")],
                               daily_sessions[(day, root_id)]):
                    values[0] += node["tokens"]
                    values[1] += amount
                    values[2] = values[2] and known
                for name, calls in tools_by_session.get(str(node["id"]), {}).items():
                    daily_tools[(day, name)] += calls
        if index >= MAX_SESSIONS:
            continue

        project_path = root.get("cwd") or directories.get(root_id) or ""
        projects.add(project_path or "(unknown)")
        _root_cost, root_known = _cost(root)
        started, ended = root["started_at"], root.get("ended_at") or root.get("last_activity_at")
        tool_calls: dict[str, int] = defaultdict(int)
        for node in nodes:
            for name, calls in tools_by_session.get(str(node["id"]), {}).items():
                tool_calls[name] += calls
        descendants = [node for node in nodes if node is not root]
        tree_tokens = sum(node["tokens"] for node in nodes)
        sessions.append({
            "id": root_id,
            "key": f"{SOURCE_ID}:{root_id}",
            "agent": SOURCE_ID,
            "project": project_path.rstrip("/").rsplit("/", 1)[-1] if project_path else "(unknown)",
            "project_path": project_path or None,
            "started_at": _iso(started),
            "ended_at": _iso(ended),
            "duration_sec": (
                max(0, int(float(ended) - float(started)))
                if isinstance(started, (int, float)) and isinstance(ended, (int, float)) else None
            ),
            "model": root.get("model"),
            "agent_version": None,
            "turns": sum(turns_by_session.get(str(node["id"]), 0) for node in nodes),
            "fresh": sum(node["fresh"] for node in nodes),
            "output": sum(node["output"] for node in nodes),
            "cache_read": sum(node["cache_read"] for node in nodes),
            "cache_write": sum(node["cache_write"] for node in nodes),
            "tokens": tree_tokens,
            "own_tokens": root["tokens"],
            "total_tokens": tree_tokens,
            "unclassified": 0,
            "breakdown_known": True,
            "cost": sum(_cost(node)[0] for node in nodes),
            "cost_known": root_known and all(_cost(node)[1] for node in descendants),
            "tools": [
                {"name": name, "calls": calls, "errors": 0}
                for name, calls in sorted(tool_calls.items(), key=lambda item: (-item[1], item[0]))
            ][:MAX_TOOLS_PER_SESSION],
            "subagents": [
                {
                    "id": str(node["id"]),
                    "tokens": node["tokens"],
                    "own_tokens": node["tokens"],
                    "total_tokens": node["tokens"],
                    "unclassified": 0,
                    "breakdown_known": True,
                    "cost": _cost(node)[0],
                    "cost_known": _cost(node)[1],
                    "turns": turns_by_session.get(str(node["id"]), 0),
                }
                for node in descendants
            ],
        })

    kept_ids = {row["id"] for row in sessions}
    return {
        "source": {
            "id": SOURCE_ID,
            "label": SOURCE_LABEL,
            "cost_status": COST_STATUS,
            "profiles": len(databases),
            "unreadable_databases": len(unreadable),
        },
        "meta_priority": META_PRIORITY,
        "meta": {"db_path": str(HERMES_DIR), "schema_version": None, "pricing_version": None},
        "totals": {
            "sessions": len(meaningful),
            "tokens": total_tokens,
            "cost_usd": total_cost,
            "projects": len(projects),
            "sessions_by_agent": {SOURCE_ID: len(meaningful)},
        },
        "project_keys": sorted(projects),
        "sessions": sessions,
        "daily_models": [
            {
                "day": day, "agent": SOURCE_ID, "model": model,
                "tokens": int(values[0]), "unclassified": 0, "breakdown_known": True,
                "cost": values[1], "cost_known": values[2],
            }
            for (day, model), values in sorted(daily_models.items())
        ],
        "daily_sessions": [
            {
                "day": day, "agent": SOURCE_ID, "session_id": session_id,
                "session_key": f"{SOURCE_ID}:{session_id}",
                "tokens": int(values[0]), "unclassified": 0, "breakdown_known": True,
                "cost": values[1], "cost_known": values[2],
            }
            for (day, session_id), values in sorted(daily_sessions.items())
            if session_id in kept_ids
        ],
        "daily_tools": [
            {"day": day, "agent": SOURCE_ID, "name": name, "calls": calls, "errors": 0}
            for (day, name), calls in sorted(daily_tools.items())
        ],
    }
