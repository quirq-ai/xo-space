"""
``~/.quirq/workspace/stats.json`` — workspace stats = sum of every project's
runtime ``stats.json``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from services.cowork_agent.project_layout import runtime_read_path, workspace_runtime_dir
from services.cowork_agent.visualizer.atomic_write import ChangeGate
from services.cowork_agent.visualizer.reader import read_json
from services.cowork_agent.visualizer.workspace_index import list_project_ids


# Match the per-project sink's bound so the workspace file doesn't
# accumulate older dates than any single project tracks.
_BY_DAY_MAX_ENTRIES = 35

# Match the per-project latency reservoir cap. Concat-then-trim loses
# some statistical fidelity for very high-traffic days, but it's
# unbiased enough for a workspace-tier estimate and avoids the
# complexity of weighted reservoir merging.
_LATENCY_RESERVOIR_CAP = 100


_gate = ChangeGate()


def reset_caches() -> None:
    """Drop the write-on-change baseline. For tests, and for a root switch."""
    _gate.reset()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_window() -> dict:
    return {
        "tokens": {"input": 0, "output": 0},
        "by_model": {},
        "by_tool": {},
        "files_edited": 0,
        "sessions": 0,
        "active_minutes": 0,
    }


def _empty_day_bucket() -> dict:
    """Same shape as sinks.stats._empty_day_bucket (kept local to avoid
    a cross-module import that would couple workspace to sink internals)."""
    return {
        "tokens":   {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "messages": {"total": 0, "user": 0, "assistant": 0,
                     "toolCalls": 0, "toolResults": 0, "errors": 0},
        "by_model": {},
    }


def _merge_window(into: dict, src: dict) -> None:
    """Sum one rolling-window dict into another in place."""
    src_tokens = src.get("tokens") or {}
    into["tokens"]["input"]  += int(src_tokens.get("input", 0) or 0)
    into["tokens"]["output"] += int(src_tokens.get("output", 0) or 0)
    into["files_edited"]   += int(src.get("files_edited", 0) or 0)
    into["sessions"]       += int(src.get("sessions", 0) or 0)
    into["active_minutes"] += int(src.get("active_minutes", 0) or 0)
    src_models = src.get("by_model") or {}
    if isinstance(src_models, dict):
        for model, mt in src_models.items():
            if not isinstance(mt, dict):
                continue
            bm = into["by_model"].setdefault(model, {"input": 0, "output": 0})
            bm["input"]  += int(mt.get("input", 0) or 0)
            bm["output"] += int(mt.get("output", 0) or 0)
    src_tools = src.get("by_tool") or {}
    if isinstance(src_tools, dict):
        for tool, n in src_tools.items():
            into["by_tool"][tool] = int(into["by_tool"].get(tool, 0)) + int(n or 0)


def _merge_day_bucket(into: dict, src: dict) -> None:
    """Sum one day_bucket into another in place."""
    src_tokens = src.get("tokens") or {}
    for k in ("input", "output", "cache_read", "cache_write"):
        into["tokens"][k] += int(src_tokens.get(k, 0) or 0)
    src_msgs = src.get("messages") or {}
    for k in ("total", "user", "assistant", "toolCalls", "toolResults", "errors"):
        into["messages"][k] += int(src_msgs.get(k, 0) or 0)
    src_models = src.get("by_model") or {}
    if isinstance(src_models, dict):
        for model, mt in src_models.items():
            if not isinstance(mt, dict):
                continue
            bm = into["by_model"].setdefault(model, {"input": 0, "output": 0, "count": 0})
            bm["input"]  += int(mt.get("input", 0) or 0)
            bm["output"] += int(mt.get("output", 0) or 0)
            bm["count"]  += int(mt.get("count", 0) or 0)
    # Merge latency: sum counts/sum_ms, take min/max extremes, concat
    # reservoirs and trim. The trim drops fidelity on high-traffic
    # days but stays unbiased enough for a workspace-tier estimate.
    src_lat = src.get("latency")
    if isinstance(src_lat, dict) and int(src_lat.get("count", 0) or 0) > 0:
        dst_lat = into.get("latency")
        if dst_lat is None:
            dst_lat = {
                "count":      int(src_lat.get("count", 0) or 0),
                "sum_ms":     int(src_lat.get("sum_ms", 0) or 0),
                "min_ms":     int(src_lat.get("min_ms", 0) or 0),
                "max_ms":     int(src_lat.get("max_ms", 0) or 0),
                "p95_sample": list(src_lat.get("p95_sample") or [])[:_LATENCY_RESERVOIR_CAP],
            }
            into["latency"] = dst_lat
        else:
            dst_lat["count"]  += int(src_lat.get("count", 0) or 0)
            dst_lat["sum_ms"] += int(src_lat.get("sum_ms", 0) or 0)
            sm = int(src_lat.get("min_ms", 0) or 0)
            if sm > 0 and (dst_lat["min_ms"] == 0 or sm < dst_lat["min_ms"]):
                dst_lat["min_ms"] = sm
            xm = int(src_lat.get("max_ms", 0) or 0)
            if xm > dst_lat["max_ms"]:
                dst_lat["max_ms"] = xm
            merged = dst_lat["p95_sample"] + list(src_lat.get("p95_sample") or [])
            dst_lat["p95_sample"] = merged[:_LATENCY_RESERVOIR_CAP]


def _trim_oldest(buckets: dict, *, max_entries: int) -> dict:
    if len(buckets) <= max_entries:
        return buckets
    keep = sorted(buckets.keys(), reverse=True)[:max_entries]
    return {k: buckets[k] for k in keep}


def apply(project_ids: Sequence[str] | None = None) -> bool:
    """Recompute workspace ``stats.json``. Returns ``True`` iff it changed."""
    rolling = {"7d": _empty_window(), "30d": _empty_window()}
    by_session: dict[str, dict] = {}
    by_runtime: dict[str, dict] = {}
    by_day: dict[str, dict] = {}

    for pid in (project_ids if project_ids is not None else list_project_ids()):
        # Runtime tier since T19, with a read-through to the pre-move copy so a
        # project that has not produced an event since the move still
        # contributes its totals.
        path = runtime_read_path(pid, "stats.json")
        st = read_json(path) if path is not None else None
        if not isinstance(st, dict):
            continue
        r = st.get("rolling") or {}
        if isinstance(r.get("7d"), dict):
            _merge_window(rolling["7d"], r["7d"])
        if isinstance(r.get("30d"), dict):
            _merge_window(rolling["30d"], r["30d"])
        bs = st.get("by_session") or {}
        if isinstance(bs, dict):
            for sid, sdata in bs.items():
                if isinstance(sdata, dict):
                    by_session[sid] = sdata  # session-ids globally unique
        br = st.get("by_runtime") or {}
        if isinstance(br, dict):
            for rt, rdata in br.items():
                if not isinstance(rdata, dict):
                    continue
                bucket = by_runtime.setdefault(rt, _empty_window())
                _merge_window(bucket, rdata)
        # Union by_day across projects. Schema 1 files omit this key
        # — `or {}` keeps backward compatibility.
        bd = st.get("by_day") or {}
        if isinstance(bd, dict):
            for date, day in bd.items():
                if not isinstance(day, dict):
                    continue
                dst = by_day.setdefault(date, _empty_day_bucket())
                _merge_day_bucket(dst, day)

    by_day = _trim_oldest(by_day, max_entries=_BY_DAY_MAX_ENTRIES)

    payload = {
        "schema": 2,
        "updated_at": _now_iso(),
        "rolling": rolling,
        "by_session": by_session,
        "by_runtime": by_runtime,
        "by_day": by_day,
    }
    target = workspace_runtime_dir() / "stats.json"
    return _gate.publish(target, payload)

