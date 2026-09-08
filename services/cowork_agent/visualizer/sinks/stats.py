"""``stats.json`` sink — rolling 7d/30d + by_runtime + by_session.

Per the stats schema (``services/cowork_agent/visualizer/schema/
stats.schema.json``):

* ``rolling.7d`` / ``rolling.30d`` — totals over the trailing window
  with ``{tokens, by_model, files_edited, sessions, active_minutes}``.
* ``by_session`` — per native session id, ``{tokens, files,
  duration_ms}``.
* ``by_runtime`` — totals bucketed by runtime (claude_code / openclaw).

The sink rebuilds from durable state stored INSIDE ``stats.json``
itself: a private ``_session_totals`` dict that's filtered on read
into the public ``by_session`` view. That way restart-after-crash
recovers state from disk; we don't need a sidecar.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from services.cowork_agent.visualizer.atomic_write import write_json_atomic
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
    UsageObserved,
)
from services.cowork_agent.visualizer.reader import read_json


_STATS_FILE = Path("stats.json")

# 30d analytics window + 5d buffer so a tick that runs near a date
# boundary doesn't drop the trailing edge users may still query.
_BY_DAY_MAX_ENTRIES = 35


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_dt(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _empty_window() -> dict:
    return {
        "tokens":         {"input": 0, "output": 0},
        "by_model":       {},
        "by_tool":        {},
        "files_edited":   0,
        "sessions":       0,
        "active_minutes": 0,
    }


def _empty_session_totals() -> dict:
    return {
        "tokens":          {"input": 0, "output": 0},
        "files":           [],
        "duration_ms":     0,
        "first_ts":        None,
        "last_ts":         None,
        "runtime":         "",
        "by_model_tokens": {},   # {model: {input, output}}
        "tools":           {},   # {tool_name: count}
    }


def _empty_day_bucket() -> dict:
    """Shape of one ``by_day`` entry. See schema 2 / stats.schema.json
    `definitions.day_bucket`. The ``latency`` sub-block is created
    lazily on the first sample (most days have no latency data when
    the source can't derive it), so it isn't seeded here.
    """
    return {
        "tokens":   {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "messages": {"total": 0, "user": 0, "assistant": 0,
                     "toolCalls": 0, "toolResults": 0, "errors": 0},
        "by_model": {},  # {model: {input, output, count}}
    }


# Per-day latency reservoir cap. Same value used at the workspace tier
# when concatenating per-project samples — see workspace/stats.py.
_LATENCY_RESERVOIR_CAP = 100


def _empty_latency() -> dict:
    return {
        "count":      0,
        "sum_ms":     0,
        "min_ms":     0,
        "max_ms":     0,
        "p95_sample": [],
    }


def _accumulate_latency(lat: dict, latency_ms: int) -> None:
    """In-place: add one latency sample to a day's latency block.

    Reservoir sampling for ``p95_sample`` (Vitter R algorithm):
    while the reservoir isn't full, append. Once full, accept with
    probability cap/count and overwrite a random slot. Keeps the
    sample distribution unbiased across the whole day's events,
    bounded memory regardless of traffic volume."""
    import random
    n = int(lat["count"]) + 1
    lat["count"] = n
    lat["sum_ms"] = int(lat["sum_ms"]) + int(latency_ms)
    if n == 1:
        lat["min_ms"] = int(latency_ms)
        lat["max_ms"] = int(latency_ms)
    else:
        if latency_ms < int(lat["min_ms"]):
            lat["min_ms"] = int(latency_ms)
        if latency_ms > int(lat["max_ms"]):
            lat["max_ms"] = int(latency_ms)
    sample = lat["p95_sample"]
    if len(sample) < _LATENCY_RESERVOIR_CAP:
        sample.append(int(latency_ms))
    else:
        i = random.randint(0, n - 1)
        if i < _LATENCY_RESERVOIR_CAP:
            sample[i] = int(latency_ms)


def _iso_to_date(ts: str) -> str | None:
    """ISO-8601 timestamp → YYYY-MM-DD in UTC. ``None`` on parse fail."""
    d = _iso_to_dt(ts)
    if d is None:
        return None
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _trim_oldest(buckets: dict, *, max_entries: int) -> dict:
    """Keep only the ``max_entries`` newest date keys (lexicographic
    sort works because all keys are ``YYYY-MM-DD``)."""
    if len(buckets) <= max_entries:
        return buckets
    keep = sorted(buckets.keys(), reverse=True)[:max_entries]
    return {k: buckets[k] for k in keep}


def apply(
    root: Path, events: Iterable[Event], *, legacy_root: Optional[Path] = None
) -> bool:
    """Apply events to the project's stats file. Returns ``True`` if
    the file changed.

    ``root`` is the project's RUNTIME directory (``~/.quirq/projects/<key>/``)
    since the tier move — ``stats.json`` is a machine-local accumulator, and
    it shares a root with the offsets that protect it (syncplan §2, R-TIER).

    ``legacy_root`` is the project's ``.xo/``, read **once**: if the runtime
    file does not exist yet but the pre-move one does, the accumulator is
    seeded from it and the very next write lands in the runtime tier. That is
    the whole migration — read-through plus copy-on-first-write, the same
    shape ``~/.xo-cowork`` → ``~/.quirq`` used, with no startup mover to race
    the first tick.
    """
    events = list(events)
    if not events:
        return False

    path = root / _STATS_FILE
    current = read_json(path)
    if current is None and legacy_root is not None:
        current = read_json(legacy_root / _STATS_FILE)
    current = current or {}
    private = dict(current.get("_session_totals") or {})
    # Private accumulator for by_day — persisted in the same file so
    # restart-after-crash recovers state without a sidecar (same
    # pattern as _session_totals).
    by_day_priv: dict = dict(current.get("_by_day_totals") or {})

    changed = False

    for ev in events:
        nsid = ev.native_session_id
        if not nsid:
            continue
        st = private.setdefault(nsid, _empty_session_totals())
        if not st["runtime"]:
            st["runtime"] = ev.runtime

        # Stamp first/last seen
        if isinstance(ev, SessionFirstSeen) or st["first_ts"] is None:
            st["first_ts"] = st["first_ts"] or ev.ts
        st["last_ts"] = ev.ts

        # ── Per-day bucketing ──
        # Done alongside the per-session work so the same event walk
        # populates both. Sessions whose events were processed before
        # by_day existed don't get retroactive buckets — forward-only.
        date = _iso_to_date(ev.ts)
        day_bucket = by_day_priv.setdefault(date, _empty_day_bucket()) if date else None

        if isinstance(ev, MessageObserved):
            if day_bucket is not None:
                msgs = day_bucket["messages"]
                msgs["total"] = int(msgs["total"]) + 1
                role_key = ev.role if ev.role in ("user", "assistant") else "assistant"
                msgs[role_key] = int(msgs.get(role_key, 0)) + 1
            changed = True

        elif isinstance(ev, UsageObserved):
            st["tokens"]["input"]  = int(st["tokens"]["input"])  + ev.input_tokens
            st["tokens"]["output"] = int(st["tokens"]["output"]) + ev.output_tokens
            model = ev.model or ""
            if model:
                m = st["by_model_tokens"].setdefault(model, {"input": 0, "output": 0})
                m["input"]  += ev.input_tokens
                m["output"] += ev.output_tokens

            if day_bucket is not None:
                tk = day_bucket["tokens"]
                tk["input"]       = int(tk["input"])       + int(ev.input_tokens or 0)
                tk["output"]      = int(tk["output"])      + int(ev.output_tokens or 0)
                tk["cache_read"]  = int(tk["cache_read"])  + int(ev.cache_read_input_tokens or 0)
                tk["cache_write"] = int(tk["cache_write"]) + int(ev.cache_creation_input_tokens or 0)
                if model:
                    mb = day_bucket["by_model"].setdefault(model, {"input": 0, "output": 0, "count": 0})
                    mb["input"]  = int(mb["input"])  + int(ev.input_tokens or 0)
                    mb["output"] = int(mb["output"]) + int(ev.output_tokens or 0)
                    mb["count"]  = int(mb["count"])  + 1
                # Latency accumulation. Sources that can't derive it
                # (hermes today) emit None and the day's latency
                # block stays absent.
                if ev.latency_ms is not None:
                    lat = day_bucket.setdefault("latency", _empty_latency())
                    _accumulate_latency(lat, int(ev.latency_ms))
            changed = True

        elif isinstance(ev, ToolUseObserved):
            # Per-day toolCalls aggregate + per-session per-tool
            # tally. The latter feeds the window by_tool rollup below.
            if day_bucket is not None:
                day_bucket["messages"]["toolCalls"] = int(day_bucket["messages"]["toolCalls"]) + 1
            if ev.tool:
                st["tools"][ev.tool] = int(st["tools"].get(ev.tool, 0)) + 1
            changed = True

        elif isinstance(ev, FileTouched):
            if ev.relative_path and ev.relative_path not in st["files"]:
                st["files"].append(ev.relative_path)
            changed = True

    if not changed:
        return False

    # Evict oldest days so the file stays bounded (~7 KB max).
    by_day_priv = _trim_oldest(by_day_priv, max_entries=_BY_DAY_MAX_ENTRIES)

    # Compute duration_ms per session
    for st in private.values():
        d1 = _iso_to_dt(st.get("first_ts") or "")
        d2 = _iso_to_dt(st.get("last_ts") or "")
        if d1 and d2 and d2 >= d1:
            st["duration_ms"] = int((d2 - d1).total_seconds() * 1000)

    # ── Build the public stats shape ────────────────────────────────────
    # by_session exposes tools + by_model so per-session BFF endpoints
    # can populate toolUsage / modelUsage without a second pass over
    # the private state.
    by_session = {
        sid: {
            "tokens":      dict(s["tokens"]),
            "files":       list(s["files"]),
            "duration_ms": int(s["duration_ms"]),
            "tools":       dict(s.get("tools") or {}),
            "by_model":    {m: dict(t) for m, t in (s.get("by_model_tokens") or {}).items()},
        }
        for sid, s in private.items()
    }

    by_runtime: dict[str, dict] = {}
    for s in private.values():
        rt = s["runtime"] or "unknown"
        bucket = by_runtime.setdefault(rt, _empty_window())
        bucket["tokens"]["input"]  += s["tokens"]["input"]
        bucket["tokens"]["output"] += s["tokens"]["output"]
        bucket["files_edited"] += len(s["files"])
        bucket["sessions"] += 1
        bucket["active_minutes"] += s["duration_ms"] // 60_000
        for model, mt in s["by_model_tokens"].items():
            bm = bucket["by_model"].setdefault(model, {"input": 0, "output": 0})
            bm["input"]  += mt["input"]
            bm["output"] += mt["output"]
        for tool, n in (s.get("tools") or {}).items():
            bucket["by_tool"][tool] = int(bucket["by_tool"].get(tool, 0)) + int(n)

    # ── Rolling 7d / 30d windows ────────────────────────────────────────
    # Each session contributes to a window if its `last_ts` is inside
    # the window. Cheap and good enough; per-event bucketing lives in
    # by_day above.
    now = datetime.now(timezone.utc)
    rolling = {"7d": _empty_window(), "30d": _empty_window()}

    for s in private.values():
        last = _iso_to_dt(s.get("last_ts") or "")
        if last is None:
            continue
        age = (now - last).total_seconds()
        for window, window_seconds in (("7d", 7 * 86400), ("30d", 30 * 86400)):
            if age <= window_seconds:
                w = rolling[window]
                w["tokens"]["input"]  += s["tokens"]["input"]
                w["tokens"]["output"] += s["tokens"]["output"]
                w["files_edited"] += len(s["files"])
                w["sessions"] += 1
                w["active_minutes"] += s["duration_ms"] // 60_000
                for model, mt in s["by_model_tokens"].items():
                    bm = w["by_model"].setdefault(model, {"input": 0, "output": 0})
                    bm["input"]  += mt["input"]
                    bm["output"] += mt["output"]
                for tool, n in (s.get("tools") or {}).items():
                    w["by_tool"][tool] = int(w["by_tool"].get(tool, 0)) + int(n)

    payload = {
        "schema": 2,
        "updated_at": _now_iso(),
        "rolling": rolling,
        "by_session": by_session,
        "by_runtime": by_runtime,
        "by_day": by_day_priv,  # public — already in the right shape, no projection needed
        "_session_totals": private,  # private — stripped on serialise by BFF route allowlist
        "_by_day_totals": by_day_priv,  # private alias for forward-compat / explicit semantics
    }
    write_json_atomic(path, payload)
    return True
