"""Read side of ``audr.jsonl``: one project's usage and cost, summarised.

The watcher's audr sink (``sinks/audr.py``) appends one OpenAudr record per
model turn and tool call. This module folds the file (and its rotations) into
the totals the Activity page shows: cost, tokens, and breakdowns by model,
session and tool, plus the newest records.

AUDR's own rules are applied, not bypassed:

* a record whose ``record_id`` was already seen is a duplicate and counts once;
* a record named by another record's ``corrects`` is replaced by it.

A model record without ``cost`` was not priced (unknown model, or written
before pricing existed). It still counts toward tokens and turns and is
reported as unpriced, never as $0.

Parsing a file costs a read of up to ~48 MB (8 MB x the current file + 5
rotations), so the fold is cached against each file's size and mtime.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

AUDR_FILE = "audr.jsonl"
RECENT_LIMIT = 50

_cache_lock = threading.Lock()
_cache: dict[Path, tuple[tuple, dict]] = {}


def _files(root: Path) -> list[Path]:
    """Rotations oldest first, then the live file."""
    rotated = sorted(root.glob("audr.*.jsonl"))
    live = root / AUDR_FILE
    return rotated + ([live] if live.is_file() else [])


def _signature(files: Iterable[Path]) -> tuple:
    sig = []
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        sig.append((path.name, st.st_size, st.st_mtime_ns))
    return tuple(sig)


def _records(files: Iterable[Path]) -> tuple[list[dict], int]:
    """Every readable record, in file order, and how many lines were not."""
    records: list[dict] = []
    unreadable = 0
    for path in files:
        try:
            handle = open(path, encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    unreadable += 1
                    continue
                if isinstance(record, dict) and isinstance(record.get("record_id"), str):
                    records.append(record)
                else:
                    unreadable += 1
    return records, unreadable


def _effective(records: list[dict]) -> list[dict]:
    """Deduplicate by ``record_id`` and apply ``corrects`` (AUDR §3.3)."""
    by_id: dict[str, dict] = {}
    for record in records:
        by_id.setdefault(record["record_id"], record)
    corrected = {r["corrects"] for r in by_id.values() if isinstance(r.get("corrects"), str)}
    return [r for rid, r in by_id.items() if rid not in corrected]


def _num(value) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _cost_of(record: dict) -> Optional[float]:
    cost = record.get("cost")
    if isinstance(cost, dict) and cost.get("currency") == "USD":
        total = cost.get("total_cost")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            return float(total)
    return None


def _empty_tokens() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def _add_tokens(into: dict, llm: dict) -> None:
    into["input"] += int(_num(llm.get("input_tokens")))
    into["output"] += int(_num(llm.get("output_tokens")))
    into["cache_read"] += int(_num(llm.get("cache_read_tokens")))
    into["cache_write"] += int(_num(llm.get("cache_write_tokens")))


def _fold(records: list[dict], unreadable: int) -> dict:
    tokens = _empty_tokens()
    total_cost = 0.0
    turns = tool_calls = priced = unpriced = 0
    models: dict[str, dict] = {}
    sessions: dict[str, dict] = {}
    tools: dict[str, int] = {}

    for record in records:
        resource = record.get("resource") if isinstance(record.get("resource"), dict) else {}
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        run = record.get("run") if isinstance(record.get("run"), dict) else {}
        timing = record.get("timing") if isinstance(record.get("timing"), dict) else {}
        run_id = str(run.get("run_id") or "")
        ts = str(timing.get("event_time") or "")
        session = sessions.setdefault(run_id, {
            "run_id": run_id, "turns": 0, "tool_calls": 0, "cost": 0.0,
            "unpriced_turns": 0, "first_at": ts, "last_at": ts,
        })
        if ts:
            session["first_at"] = min(filter(None, (session["first_at"], ts)))
            session["last_at"] = max(session["last_at"], ts)

        llm = usage.get("llm")
        if resource.get("type") == "model" and isinstance(llm, dict):
            turns += 1
            session["turns"] += 1
            _add_tokens(tokens, llm)
            name = str(resource.get("name") or "unknown")
            model = models.setdefault(name, {
                "model": name, "provider": str(resource.get("provider") or ""),
                "turns": 0, "tokens": _empty_tokens(), "cost": 0.0, "unpriced_turns": 0,
            })
            model["turns"] += 1
            _add_tokens(model["tokens"], llm)
            cost = _cost_of(record)
            if cost is None:
                unpriced += 1
                model["unpriced_turns"] += 1
                session["unpriced_turns"] += 1
            else:
                priced += 1
                total_cost += cost
                model["cost"] += cost
                session["cost"] += cost
        elif resource.get("type") == "tool":
            calls = int(_num((usage.get("tool") or {}).get("call_count"))) or 1
            tool_calls += calls
            session["tool_calls"] += calls
            name = str(resource.get("name") or "unknown")
            tools[name] = tools.get(name, 0) + calls

    def money(value: float) -> float:
        return round(value, 9)

    for entry in (*models.values(), *sessions.values()):
        entry["cost"] = money(entry["cost"])

    recent = []
    for record in sorted(records, key=lambda r: str((r.get("timing") or {}).get("event_time") or ""),
                         reverse=True)[:RECENT_LIMIT]:
        resource = record.get("resource") or {}
        usage = record.get("usage") or {}
        llm = usage.get("llm") if isinstance(usage.get("llm"), dict) else None
        recent.append({
            "record_id": record["record_id"],
            "event_time": str((record.get("timing") or {}).get("event_time") or ""),
            "run_id": str((record.get("run") or {}).get("run_id") or ""),
            "type": str(resource.get("type") or ""),
            "name": str(resource.get("name") or ""),
            "provider": str(resource.get("provider") or ""),
            "input_tokens": int(_num(llm.get("input_tokens"))) if llm else None,
            "output_tokens": int(_num(llm.get("output_tokens"))) if llm else None,
            "cost": _cost_of(record),
        })

    return {
        "records": len(records),
        "unreadable_lines": unreadable,
        "currency": "USD",
        "total_cost": money(total_cost),
        "model_turns": turns,
        "priced_turns": priced,
        "unpriced_turns": unpriced,
        "tool_calls": tool_calls,
        "tokens": tokens,
        "by_model": sorted(models.values(), key=lambda m: (-m["cost"], -m["turns"], m["model"])),
        "by_session": sorted(sessions.values(), key=lambda s: s["last_at"], reverse=True),
        "by_tool": [{"tool": t, "calls": c}
                    for t, c in sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))],
        "recent": recent,
    }


def summarize(root: Optional[Path]) -> dict:
    """The project's usage summary; zeros when nothing was recorded yet."""
    files = _files(root) if root is not None and root.is_dir() else []
    sig = _signature(files)
    if root is not None:
        with _cache_lock:
            hit = _cache.get(root)
        if hit is not None and hit[0] == sig:
            return hit[1]
    records, unreadable = _records(files)
    summary = _fold(_effective(records), unreadable)
    if root is not None:
        with _cache_lock:
            _cache[root] = (sig, summary)
    return summary
