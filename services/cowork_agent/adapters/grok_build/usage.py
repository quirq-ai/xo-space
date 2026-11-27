"""
Grok Build usage: discovery + parser + dashboard, over Grok's own usage ledger.

Grok keeps, per session, ``~/.grok/sessions/<encoded-cwd>/<id>/usage.json`` (what
``grok usage <id>`` prints): session totals and one record per turn, each with
its ``endedAt`` and per-model ``modelUsage``. Per Grok's docs
(``docs/user-guide/17-sessions.md``, ``14-headless-mode.md``):

  * ``inputTokens`` there is the **full** prompt (cache hits included), so fresh
    input = ``inputTokens - cachedReadTokens - cacheCreationTokens``;
    ``outputTokens`` includes ``reasoningTokens``.
  * ``costUsdTicks`` is the cost in ticks, 10**10 per USD. A turn without it has
    an unreported cost (never a free one): its entry's cost is empty.
  * A session's ledger includes history inherited by resume or fork, so a turn
    is identified by its end time, number and model and counted once, in the
    oldest session that holds it.
  * A turn's usage includes the sub-agents that finished in it, so a
    sub-agent's own session (listed under its parent's ``subagents/``) is not
    counted again.

Each (turn, model) is one assistant entry at the turn's end; each prompt in
``updates.jsonl`` (its time only; no text is read out) is a user entry. A turn
that ended with no usage record (cancelled, failed) has no entry, never zero
tokens.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Iterator, Optional

from services.cowork_agent.adapters import usage_common as _uc
from services.cowork_agent.adapters.grok_build.history import summaries

PROVIDER = "xai"
TICKS_PER_USD = 10 ** 10
_USAGE = "usage.json"
_UPDATES = "updates.jsonl"


# ── Discovery ─────────────────────────────────────────────────────────────────


def sessions() -> list[tuple[dict, Path]]:
    """Every top-level session (not a sub-agent's), oldest first."""
    found = list(summaries())
    children = {meta.parent.name for _, folder in found for meta in (folder / "subagents").glob("*/meta.json")}
    found = [(summary, folder) for summary, folder in found if folder.name not in children]
    found.sort(key=lambda item: (str(item[0].get("created_at") or ""), item[1].name))
    return found


def _discover() -> list[str]:
    """Each session's ledger (``usage.json``), or its update log while it has none yet."""
    out: list[str] = []
    for _summary, folder in sessions():
        for name in (_USAGE, _UPDATES):
            if (folder / name).is_file():
                out.append(str(folder / name))
                break
    return out


def get_session_files(*, agent_id: Optional[str] = None) -> list[str]:
    return _discover()


def session_of(path: str) -> str:
    return Path(path).parent.name


# ── One session, read once per change ─────────────────────────────────────────


def _int(record: dict, key: str) -> int:
    value = record.get(key)
    return max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


_FRACTION = re.compile(r"(\.\d{6})\d+")


def _time(stamp: object) -> Optional[datetime]:
    """``2026-11-27T09:26:04.507116371+00:00`` (nanoseconds) as an aware datetime."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        moment = datetime.fromisoformat(_FRACTION.sub(r"\1", stamp.replace("Z", "+00:00")))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _updates(updates: Path, since: Optional[datetime]) -> tuple[list[float], list[float]]:
    """When each prompt was sent (a run of ``user_message_chunk`` updates is one
    prompt), and when each turn ended that Grok recorded no usage for (one
    cancelled or failed before its usage arrived). Only times are kept; a
    forked session's inherited history (before ``since``) is its parent's."""
    prompts: list[float] = []
    unreported: list[float] = []
    in_prompt = False
    floor = since.timestamp() if since else None
    try:
        stream = updates.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return prompts, unreported
    with stream:
        for line in stream:
            if '"sessionUpdate"' not in line:
                continue
            try:
                row = json.loads(line)
                update = row["params"]["update"]
                kind = update["sessionUpdate"]
            except (ValueError, KeyError, TypeError):
                continue
            stamp = row.get("timestamp")
            if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or (floor is not None and stamp < floor):
                in_prompt = kind == "user_message_chunk"
                continue
            if kind == "user_message_chunk":
                if not in_prompt:
                    prompts.append(float(stamp))
                in_prompt = True
                continue
            in_prompt = False
            if kind == "turn_completed" and not isinstance(update.get("usage"), dict):
                unreported.append(float(stamp))
    return prompts, unreported


def _replies(turn: dict) -> Iterator[tuple[str, dict]]:
    """(model, its usage) for each model a turn used."""
    models = turn.get("modelUsage")
    if isinstance(models, dict) and models:
        for model, usage in models.items():
            if isinstance(usage, dict):
                yield str(model), usage
        return
    yield str(turn.get("primaryModelId") or "grok"), turn


def _entry(at: datetime, model: str, usage: dict, key: str, duration_ms: Optional[int]) -> dict:
    full = _int(usage, "inputTokens")
    cache_read = _int(usage, "cachedReadTokens")
    cache_write = _int(usage, "cacheCreationTokens")
    fresh = max(0, full - cache_read - cache_write)
    output = _int(usage, "outputTokens")
    ticks = usage.get("costUsdTicks")
    known = isinstance(ticks, (int, float)) and not isinstance(ticks, bool) and ticks >= 0
    return {
        "role": "assistant",
        "usage": {
            "input": fresh, "output": output, "cacheRead": cache_read, "cacheWrite": cache_write,
            "totalTokens": fresh + output + cache_read + cache_write,
            # Grok reports a cost per call, not its parts; an unreported cost stays empty.
            "cost": {"total": ticks / TICKS_PER_USD} if known else {},
        },
        "provider": PROVIDER,
        "model": model,
        "messageId": key,
        "timestamp": int(at.timestamp() * 1000),
        "stopReason": None,
        "toolNames": [],
        "toolResultCounts": {"total": 0, "errors": 0},
        "durationMs": duration_ms,
    }


class _Parsed:
    __slots__ = ("meta", "entries", "digest")

    def __init__(self, meta: dict, entries: list, digest: _uc.Digest) -> None:
        self.meta = meta
        self.entries = entries
        self.digest = digest


def _summary_of(folder: Path) -> dict:
    try:
        data = json.loads((folder / "summary.json").read_bytes())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _build(folder: Path) -> _Parsed:
    session_id = folder.name
    summary = _summary_of(folder)
    forked = bool(summary.get("parent_session_id"))
    try:
        ledger = json.loads((folder / _USAGE).read_bytes())
    except (OSError, ValueError):
        ledger = {}
    turns = ledger.get("turns") if isinstance(ledger, dict) else None
    prompts, _unreported = _updates(folder / _UPDATES, _time(summary.get("created_at")) if forked else None)

    timeline: list[tuple[float, int, object]] = [(at, 0, None) for at in prompts]
    for turn in turns if isinstance(turns, list) else ():
        at = _time(turn.get("endedAt")) if isinstance(turn, dict) else None
        if at is not None:
            timeline.append((at.timestamp(), 1, turn))
    timeline.sort(key=lambda item: (item[0], item[1]))

    entries: list = []
    builder = _uc.DigestBuilder()
    last_prompt: Optional[float] = None
    for seconds, kind, turn in timeline:
        at = datetime.fromtimestamp(seconds, tz=timezone.utc)
        if kind == 0:
            entries.append({"role": "user", "timestamp": int(seconds * 1000)})
            builder.user(at, at.isoformat(), counted=True)
            last_prompt = seconds
            continue
        duration_ms = int((seconds - last_prompt) * 1000) if last_prompt is not None else None
        last_prompt = None
        for model, usage in _replies(turn):
            # A turn's identity survives a fork or resume copying it into another ledger.
            key = f"{turn.get('endedAt')}|{turn.get('turnNumber')}|{model}"
            entry = _entry(at, model, usage, key, duration_ms)
            entries.append(entry)
            u = entry["usage"]
            builder.reply(at, model=model, input=u["input"], output=u["output"], cache_read=u["cacheRead"],
                          cache_write=u["cacheWrite"], tokens=u["input"] + u["output"], message_id=key)
    title = summary.get("generated_title") or summary.get("session_summary")
    meta = {"sessionId": session_id, "sessionFile": session_id}
    if isinstance(title, str) and title:
        meta["title"] = title[:80]
    return _Parsed(meta, entries, builder.build(file_id=session_id, session_id=session_id,
                                         title=meta.get("title") or "Untitled Session"))


def _stamp(folder: Path) -> tuple:
    out: list = []
    for name in (_USAGE, _UPDATES, "summary.json"):
        try:
            st = os.stat(folder / name)
            out.extend((st.st_ino, st.st_size, st.st_mtime_ns))
        except OSError:
            out.extend((0, 0, 0))
    return tuple(out)


_kept: dict[str, tuple[tuple, _Parsed]] = {}
_kept_lock = Lock()


def _parsed(path: str) -> _Parsed:
    folder = Path(path).parent
    stamp = _stamp(folder)
    with _kept_lock:
        kept = _kept.get(str(folder))
    if kept is not None and kept[0] == stamp:
        return kept[1]
    parsed = _build(folder)
    if _stamp(folder) == stamp:   # a session written while it was read is read again next time
        with _kept_lock:
            _kept[str(folder)] = stamp, parsed
    return parsed


def parse_file(path: str, *, start_ms: Optional[int] = None, end_ms: Optional[int] = None) -> tuple[dict, list]:
    """One session as ``(meta, entries)`` in the shared shape, within the window."""
    parsed = _parsed(path)
    entries = [e for e in parsed.entries
               if not (start_ms and e["timestamp"] < start_ms) and not (end_ms and e["timestamp"] > end_ms)]
    return dict(parsed.meta), entries


# ── /api/usage dashboard (UsageStats) ─────────────────────────────────────────


class _Digests:
    """``usage_common.Digests``' reader for a session, whose usage spans its
    ledger, its update log and its summary."""

    @staticmethod
    def read(paths: list[str]) -> Iterator[tuple[str, Optional[_uc.Digest]]]:
        with _kept_lock:
            for gone in _kept.keys() - {str(Path(p).parent) for p in paths}:
                del _kept[gone]
        for path in paths:
            try:
                yield path, _parsed(path).digest
            except Exception:
                yield path, None


def _add_costs(stats: dict, paths: list[str], since_ms: int, zone) -> None:
    """Grok reports cost, which the shared fold doesn't carry: add it to the
    same rows, each turn once (as the fold counts it)."""
    counted: set = set()
    total = 0.0
    unknown = 0
    models = {row["model_id"]: row for row in stats["by_model"]}
    days = {row["date"]: row for row in stats["daily"]}
    sessions = {row["session_id"]: row for row in stats["by_session"]}
    for path in paths:
        parsed = _parsed(path)
        for entry in parsed.entries:
            if entry["role"] == "user" or entry["timestamp"] < since_ms or entry["messageId"] in counted:
                continue
            counted.add(entry["messageId"])
            cost = entry["usage"]["cost"].get("total")
            if cost is None:
                unknown += 1
                continue
            total += cost
            if entry["model"] in models:
                models[entry["model"]]["total_cost"] += cost
            day = _uc.date_from_ms(entry["timestamp"], zone)
            if day in days:
                days[day]["cost"] += cost
            if parsed.meta["sessionId"] in sessions:
                sessions[parsed.meta["sessionId"]]["total_cost"] += cost
    stats["total_cost"] = round(total, 6)
    for rows, key in ((stats["by_model"], "total_cost"), (stats["daily"], "cost"), (stats["by_session"], "total_cost")):
        for row in rows:
            row[key] = round(row[key], 6)
    stats["unpriced_messages"] = unknown


def aggregate_for_dashboard(*, days: int = 30, tz: str = "local") -> dict:
    days = max(1, min(days, 365))
    zone = _uc.resolve_tz(tz)
    paths = _discover()
    stats = _uc.dashboard_from_digests(_Digests, paths, days=days, zone=zone, provider=PROVIDER)
    today = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    since_ms = int((today - timedelta(days=days - 1)).timestamp() * 1000)
    _add_costs(stats, paths, since_ms, zone)
    return stats


def dashboard(*, window: dict) -> dict:
    if "days" in window:
        return aggregate_for_dashboard(days=int(window["days"]), tz=window.get("tz", "local"))
    _s, _e, derived = _uc.window_to_ms(window)
    return aggregate_for_dashboard(days=derived or 30, tz="local")


# ── Shared entries-based views ────────────────────────────────────────────────

_SOURCE = _uc.Source(discover=lambda: get_session_files(), parse_file=parse_file, session_of=session_of)


def build_summary(session_meta: dict, entries: list) -> dict:
    return _uc.build_summary(session_meta, entries)


def analytics(*, window: dict) -> dict:
    return _uc.analytics(_SOURCE, window=window)


def summary(*, window: dict) -> dict:
    return _uc.summary(_SOURCE, window=window)


def summary_card(*, window: dict) -> dict:
    return _uc.summary_card(_SOURCE, window=window)


def list_sessions() -> dict:
    return _uc.list_sessions(_SOURCE)


def get_session(session_id: str, *, window: dict | None = None) -> dict | None:
    return _uc.get_session(_SOURCE, session_id, window=window)


def aggregate_for_sync(*, since_date: Optional[str] = None) -> dict:
    return _uc.aggregate_for_sync(_SOURCE, since_date=since_date)


sync_payload = aggregate_for_sync


__all__ = [
    "get_session_files", "parse_file", "aggregate_for_dashboard", "dashboard",
    "build_summary", "analytics", "summary", "summary_card", "list_sessions",
    "get_session", "aggregate_for_sync", "sync_payload", "session_of",
]
