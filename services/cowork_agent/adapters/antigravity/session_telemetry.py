"""Read-only Space session telemetry for local agy (Antigravity CLI) conversations.

agy indexes every conversation in ``conversation_summaries.db`` (its id, its
workspace, when it last changed, whether it is a sub-agent's); a
conversation's start is its transcript's first step. Its tokens are agy's own
per-call usage records (:mod:`.usage` over ``tokens``): fresh input, cache reads
and writes and output, each call on its own day, a sub-agent's calls in its
root conversation's row. agy records no cost, so cost is null. A call that
failed before the provider answered has no usage record: it is counted in
``unreported_calls``, not as zero tokens.

No prompt text, title or tool argument leaves this module. Every read is
read-only; a conversation's figures are kept until agy changes it.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from services.cowork_agent.adapters import usage_common as _uc
from services.cowork_agent.adapters.antigravity import usage as _usage
from services.cowork_agent.adapters.antigravity.paths import CONVERSATIONS_DIR, transcript_path
from services.cowork_agent.adapters.antigravity.transcript import _SUMMARIES_DB

SOURCE_ID = "antigravity"
SOURCE_LABEL = "Antigravity"
COST_STATUS = "unavailable"
META_PRIORITY = 30
# No path_env: agy's home comes from its manifest (``paths.AGY_HOME``), not a variable, so it isn't editable.
SOURCE_CONFIG = {
    "vendor": "google", "path_default": "~/.gemini/antigravity-cli", "path_kind": "dir",
    "path_label": "Antigravity CLI home", "collects": ["Conversations, models and tokens"],
    "never": "No prompt text stored",
}

MAX_SESSIONS = 500
_BUSY_TIMEOUT_S = 0.5
_PARTS = {"fresh": "input", "output": "output", "cache_read": "cacheRead", "cache_write": "cacheWrite"}


def _iso(value: object) -> str | None:
    """agy's ``2026-11-27 09:34:51.677697+00:00`` as ``2026-11-27T09:34:51Z``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        head, _, zone = value.partition("+")
        moment = datetime.fromisoformat(head[:26].replace(" ", "T") + ("+" + zone if zone else "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _folder(workspace_uris: object) -> str | None:
    try:
        uris = json.loads(workspace_uris) if isinstance(workspace_uris, str) else []
    except ValueError:
        return None
    for uri in uris if isinstance(uris, list) else ():
        if isinstance(uri, str) and uri.startswith("file:"):
            path = unquote(urlparse(uri).path)
            if path:
                return path
    return None


def _started(conversation_id: str) -> str | None:
    """The transcript's first step's ``created_at`` (one line read)."""
    try:
        with transcript_path(conversation_id).open(encoding="utf-8") as stream:
            first = json.loads(stream.readline() or "{}")
    except (OSError, ValueError):
        return None
    stamp = first.get("created_at") if isinstance(first, dict) else None
    return stamp if isinstance(stamp, str) else None


def _conversation(root: str, members: list[str], counted: set) -> tuple[dict, list, int]:
    """A root conversation's figures, its sub-agents' calls included."""
    replies: list = []
    prompts = 0
    unreported = 0
    model = None
    for conversation_id in members:
        meta, entries = _usage.parse_file(str(CONVERSATIONS_DIR / f"{conversation_id}.db"))
        unreported += meta.get("unreportedCalls", 0)
        for entry in _uc.counted_once(entries, counted):
            if entry["role"] == "user":
                prompts += conversation_id == root
                continue
            replies.append(entry)
            if conversation_id == root and model is None:
                model = entry["model"]
    row = {key: sum(e["usage"][source] for e in replies) for key, source in _PARTS.items()}
    tokens = sum(row.values())
    row.update({"tokens": tokens, "own_tokens": tokens, "total_tokens": tokens, "unclassified": 0,
                "turns": prompts, "unreported_calls": unreported, "model": model})
    return row, replies, unreported


def _rows(db: Path) -> list[tuple]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_S)
    try:
        return con.execute(
            "SELECT conversation_id, last_modified_time, workspace_uris, nesting_depth FROM conversation_summaries "
            "ORDER BY last_modified_time DESC").fetchall()
    finally:
        con.close()


def collect_session_telemetry():
    if not _SUMMARIES_DB.is_file():
        raise ValueError("No local Antigravity conversations")
    try:
        rows = _rows(_SUMMARIES_DB)
    except sqlite3.Error as exc:
        raise ValueError(f"Antigravity's conversation index is unreadable ({exc.__class__.__name__})") from exc
    parents = _usage._parents()
    members: dict[str, list[str]] = defaultdict(list)
    for conversation_id, _changed, _uris, _depth in rows:
        if isinstance(conversation_id, str) and conversation_id:
            members[_usage._root(conversation_id, parents)].append(conversation_id)
    sessions = []
    daily_models: dict[tuple[str, str], int] = defaultdict(int)
    daily_sessions: dict[tuple[str, str], int] = defaultdict(int)
    counted: set = set()
    for conversation_id, changed, workspace_uris, depth in rows:
        cwd = _folder(workspace_uris)
        if not isinstance(conversation_id, str) or not conversation_id or cwd is None:
            continue
        if depth or _usage._root(conversation_id, parents) != conversation_id:
            continue    # a sub-agent's conversation: in its root's row
        figures, replies, _unreported = _conversation(
            conversation_id, [conversation_id] + [c for c in members[conversation_id] if c != conversation_id],
            counted)
        for reply in replies:
            day = _uc.date_from_ms(reply["timestamp"])
            daily_models[(day, reply["model"])] += reply["usage"]["totalTokens"]
            daily_sessions[(day, conversation_id)] += reply["usage"]["totalTokens"]
        ended = _iso(changed)
        sessions.append({
            "id": conversation_id, "key": f"{SOURCE_ID}:{conversation_id}", "agent": SOURCE_ID,
            "project": Path(cwd).name, "project_path": cwd, "started_at": _started(conversation_id) or ended,
            "ended_at": ended, **figures, "cost": None, "cost_known": False, "breakdown_known": True,
            "tokens_known": True,
        })
        if len(sessions) >= MAX_SESSIONS:
            break
    if not sessions:
        raise ValueError("No local Antigravity conversations")
    kept = {row["id"] for row in sessions}
    projects = sorted({row["project_path"] for row in sessions})
    return {
        "source": {"id": SOURCE_ID, "label": SOURCE_LABEL, "cost_status": COST_STATUS},
        "meta_priority": META_PRIORITY, "project_keys": projects, "sessions": sessions,
        "totals": {"sessions": len(sessions), "projects": len(projects), "tokens": sum(r["tokens"] for r in sessions),
                   "cost_usd": None, "unreported_calls": sum(r["unreported_calls"] for r in sessions),
                   "sessions_by_agent": {SOURCE_ID: len(sessions)}},
        "daily_models": [
            {"day": day, "agent": SOURCE_ID, "model": model, "tokens": tokens, "unclassified": 0,
             "breakdown_known": True, "cost": None, "cost_known": False}
            for (day, model), tokens in sorted(daily_models.items())],
        "daily_sessions": [
            {"day": day, "agent": SOURCE_ID, "session_id": sid, "session_key": f"{SOURCE_ID}:{sid}",
             "tokens": tokens, "unclassified": 0, "breakdown_known": True, "cost": None, "cost_known": False}
            for (day, sid), tokens in sorted(daily_sessions.items()) if sid in kept],
    }
