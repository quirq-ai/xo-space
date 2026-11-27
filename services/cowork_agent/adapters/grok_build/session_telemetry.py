"""Read-only Space session telemetry for local Grok Build sessions.

Each session's figures come from Grok's own usage ledger (``usage.json``, read by
:mod:`.usage`): fresh input, cache reads and writes, output and the cost Grok
reported (``costUsdTicks``, 10**10 per USD). As in the usage views, a turn a
fork or resume copied from an older session counts in that one only, and a
sub-agent's session is not listed (its usage is in its parent's turns). A
session with no ledger yet has no known tokens: its token fields are null, not
zero. No prompt text leaves this module.
"""

from collections import defaultdict
from pathlib import Path

from . import usage as _usage

SOURCE_ID = "grok_build"
SOURCE_LABEL = "Grok Build"
COST_STATUS = "reported"
META_PRIORITY = 10
SOURCE_CONFIG = {
    "vendor": "xai", "path_env": "GROK_HOME", "path_default": "~/.grok", "path_kind": "dir",
    "path_label": "Grok Build home", "collects": ["Sessions, models, tokens and cost"],
    "never": "No prompt text stored",
}

_UNKNOWN_TOKENS = dict.fromkeys(("fresh", "output", "cache_read", "cache_write", "unclassified",
                                 "tokens", "own_tokens", "total_tokens"))


def _figures(folder: Path, counted: set) -> dict:
    """The session's token and cost fields (its turns not already in
    ``counted``), or nulls when Grok recorded none."""
    if not (folder / "usage.json").is_file():
        if not any(_usage._updates(folder / "updates.jsonl", None)):
            # Nothing was ever sent: no turn ran, so nothing was used.
            return {**dict.fromkeys(_UNKNOWN_TOKENS, 0), "cost": 0.0, "cost_known": True, "breakdown_known": True,
                    "tokens_known": True, "_replies": []}
        return {**_UNKNOWN_TOKENS, "cost": None, "cost_known": False, "breakdown_known": False,
                "tokens_known": False, "_replies": []}
    _meta, entries = _usage.parse_file(str(folder / "usage.json"))
    replies = [e for e in _usage._uc.counted_once(entries, counted) if e["role"] == "assistant"]
    parts = {"fresh": "input", "output": "output", "cache_read": "cacheRead", "cache_write": "cacheWrite"}
    row = {key: sum(e["usage"][source] for e in replies) for key, source in parts.items()}
    tokens = sum(row.values())
    priced = all("total" in e["usage"]["cost"] for e in replies)
    row.update({
        "unclassified": 0, "tokens": tokens, "own_tokens": tokens, "total_tokens": tokens,
        "cost": round(sum(e["usage"]["cost"].get("total", 0.0) for e in replies), 6) if priced else None,
        "cost_known": priced, "breakdown_known": True, "tokens_known": True,
        "turns": sum(1 for e in entries if e["role"] == "user"), "_replies": replies,
    })
    return row


def collect_session_telemetry():
    sessions = []
    daily_models: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0.0, True])
    daily_sessions: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0.0, True])
    counted: set = set()
    for summary, folder in _usage.sessions():      # oldest first, sub-agents' left out
        native = folder.name
        cwd = summary["info"]["cwd"]
        figures = _figures(folder, counted)
        for reply in figures.pop("_replies"):
            day = _usage._uc.date_from_ms(reply["timestamp"])
            tokens = reply["usage"]["totalTokens"]
            cost = reply["usage"]["cost"].get("total")
            for bucket in (daily_models[(day, reply["model"])], daily_sessions[(day, native)]):
                bucket[0] += tokens
                if cost is None:
                    bucket[2] = False
                else:
                    bucket[1] += cost
        sessions.append({
            "id": native, "key": f"{SOURCE_ID}:{native}", "agent": SOURCE_ID, "project": Path(cwd).name,
            "project_path": cwd, "started_at": summary.get("created_at"), "ended_at": summary.get("updated_at"),
            "model": summary.get("current_model_id"), **figures,
        })
    if not sessions:
        raise ValueError("No local Grok Build sessions")
    sessions.sort(key=lambda row: row["ended_at"] or "", reverse=True)
    sessions = sessions[:500]
    kept = {row["id"] for row in sessions}
    projects = sorted({row["project_path"] for row in sessions})
    known = [row for row in sessions if row["tokens_known"]]
    return {
        "source": {"id": SOURCE_ID, "label": SOURCE_LABEL, "cost_status": COST_STATUS},
        "meta_priority": META_PRIORITY, "project_keys": projects, "sessions": sessions,
        "totals": {"sessions": len(sessions), "projects": len(projects), "sessions_by_agent": {SOURCE_ID: len(sessions)},
                   "tokens": sum(row["tokens"] for row in known),
                   "tokens_complete": len(known) == len(sessions),
                   "cost_usd": round(sum(row["cost"] or 0.0 for row in known), 6),
                   "cost_complete": all(row["cost_known"] for row in known)},
        "daily_models": [
            {"day": day, "agent": SOURCE_ID, "model": model, "tokens": tokens, "unclassified": 0,
             "breakdown_known": True, "cost": round(cost, 6) if priced else None, "cost_known": priced}
            for (day, model), (tokens, cost, priced) in sorted(daily_models.items())],
        "daily_sessions": [
            {"day": day, "agent": SOURCE_ID, "session_id": sid, "session_key": f"{SOURCE_ID}:{sid}",
             "tokens": tokens, "unclassified": 0, "breakdown_known": True,
             "cost": round(cost, 6) if priced else None, "cost_known": priced}
            for (day, sid), (tokens, cost, priced) in sorted(daily_sessions.items()) if sid in kept],
    }
