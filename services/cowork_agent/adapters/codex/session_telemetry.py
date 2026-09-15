"""Read-only Space session telemetry for local Codex sessions.

Metadata and baseline cumulative token totals come from Codex's state SQLite
DB. Referenced rollout JSONL files are streamed for newer totals, per-day token
deltas, token breakdowns, turn counts, and tool names. Prompt text, titles,
tool arguments, tool output, reasoning, and assistant messages are never
selected or retained.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


SOURCE_ID = "codex"
SOURCE_LABEL = "Codex"
META_PRIORITY = 10
COST_STATUS = "unavailable"
# How Space's Configure page presents and edits this source. Read generically
# by services/telemetry_sources.py; the env key is the one _codex_home() honors.
SOURCE_CONFIG = {
    "vendor": "openai",
    "path_env": "CODEX_HOME",
    "path_default": "~/.codex",
    "path_kind": "dir",
    "path_label": "Codex home",
    "collects": ["Threads and models", "Token usage", "Tool calls"],
    "never": "No prompt text stored",
}

MAX_SESSIONS = 500
MAX_TOOLS_PER_SESSION = 10
MAX_DETAILED_ROLLOUTS = 1000
MAX_DETAILED_BYTES = 1024 * 1024 * 1024
MAX_ROLLOUT_CACHE = 1024
MAX_ZERO_TOKEN_ROOT_PROBES = 8
MAX_ZERO_TOKEN_PROBE_BYTES = 64 * 1024 * 1024
_BUSY_TIMEOUT_MS = 2000

# path -> ((size, mtime_ns, fallback_model), parsed rollout). Stable history is
# scanned once per server process; only an actively-growing rollout is re-read.
_ROLLOUT_CACHE: OrderedDict[
    str, tuple[tuple[int, int, str], dict]
] = OrderedDict()

_REQUIRED_THREAD_COLUMNS = {
    "id", "rollout_path", "created_at", "updated_at", "cwd",
    "tokens_used", "cli_version",
}

_EVENT_TYPES = (
    "token_count", "turn_context", "task_started", "function_call",
    "custom_tool_call", "mcp_tool_call_end", "exec_command_end",
    "patch_apply_end", "web_search_end", "view_image_tool_call",
    "image_generation_end",
)
_INTERESTING = tuple(
    marker
    for event_type in _EVENT_TYPES
    for marker in (
        f'"type":"{event_type}"'.encode(),
        f'"type": "{event_type}"'.encode(),
    )
)


def _codex_home() -> Path:
    configured = (os.getenv("CODEX_HOME") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def runtime_mounts() -> list[Path]:
    """Native directories the managed Docker runtime may read if present."""
    return [_codex_home()]


def _state_version(path: Path) -> int:
    match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
    return int(match.group(1)) if match else -1


def _find_state_db(root: Path) -> Path:
    candidates = sorted(
        (path for path in root.glob("state_*.sqlite") if path.is_file()),
        key=lambda path: (_state_version(path), path.stat().st_mtime_ns),
        reverse=True,
    )
    for path in candidates:
        connection = None
        try:
            connection = _connect_ro(path)
            columns = _table_columns(connection, "threads")
            if _REQUIRED_THREAD_COLUMNS <= columns:
                # Force a read so a corrupt or half-created higher-version DB
                # does not mask the newest usable state store.
                connection.execute("select 1 from threads limit 1").fetchone()
                return path
        except (OSError, sqlite3.Error):
            continue
        finally:
            if connection is not None:
                connection.close()
    if candidates:
        raise ValueError(
            f"No schema-compatible Codex state DB found under {root}"
        )
    else:
        # Keep the missing-store failure distinct from an in-progress or
        # incompatible migration so source status remains actionable.
        raise FileNotFoundError(
            f"Codex state DB not found under {root} (set CODEX_HOME to change)"
        )


def _connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute(f"pragma busy_timeout={_BUSY_TIMEOUT_MS}")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"pragma table_info({table})")}


def _load_state(path: Path) -> tuple[list[dict], dict[str, str], str | None]:
    connection = _connect_ro(path)
    try:
        columns = _table_columns(connection, "threads")
        missing = sorted(_REQUIRED_THREAD_COLUMNS - columns)
        if missing:
            raise ValueError(
                f"Codex state DB threads table is missing: {', '.join(missing)}"
            )

        def optional(name: str, fallback: str = "null") -> str:
            return name if name in columns else fallback

        # Deliberately whitelist fields. In particular, do not select title,
        # first_user_message, preview, or any other content-bearing column.
        rows = connection.execute(
            "select id, rollout_path, created_at, updated_at, cwd, tokens_used,"
            f" cli_version, {optional('model')}, {optional('created_at_ms')},"
            f" {optional('updated_at_ms')} from threads"
        ).fetchall()
        sessions = [
            {
                "id": str(sid),
                "rollout_path": str(rollout_path or ""),
                "created_at": created_at,
                "updated_at": updated_at,
                "cwd": str(cwd or ""),
                "tokens_used": int(tokens_used or 0),
                "cli_version": str(cli_version or ""),
                "model": str(model or ""),
                "created_at_ms": created_at_ms,
                "updated_at_ms": updated_at_ms,
            }
            for (sid, rollout_path, created_at, updated_at, cwd, tokens_used,
                 cli_version, model, created_at_ms, updated_at_ms) in rows
            if sid
        ]

        parent_of: dict[str, str] = {}
        if _table_columns(connection, "thread_spawn_edges"):
            for parent, child in connection.execute(
                "select parent_thread_id, child_thread_id from thread_spawn_edges"
            ):
                if parent and child and parent != child:
                    parent_of[str(child)] = str(parent)

        schema_version = None
        if _table_columns(connection, "_sqlx_migrations"):
            row = connection.execute(
                "select max(version) from _sqlx_migrations where success=1"
            ).fetchone()
            if row and row[0] is not None:
                schema_version = str(row[0])
        return sessions, parent_of, schema_version
    finally:
        connection.close()


def _epoch_seconds(seconds: Any, millis: Any) -> float:
    try:
        milliseconds = float(millis or 0)
    except (TypeError, ValueError):
        milliseconds = 0
    if milliseconds > 0:
        return milliseconds / 1000
    try:
        return float(seconds or 0)
    except (TypeError, ValueError):
        return 0


def _iso(timestamp: float) -> str | None:
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _day(value: Any) -> str:
    if not isinstance(value, str) or len(value) < 10:
        return ""
    candidate = value[:10]
    return candidate if candidate[4:5] == "-" and candidate[7:8] == "-" else ""


def _usage_snapshot(payload: dict) -> dict[str, int] | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None

    def number(key: str) -> int:
        value = usage.get(key, 0)
        return max(0, int(value)) if isinstance(value, (int, float)) else 0

    input_tokens = number("input_tokens")
    cached = min(input_tokens, number("cached_input_tokens"))
    return {
        "input": input_tokens,
        "cached": cached,
        "output": number("output_tokens"),
    }


def _response_tool_name(payload: dict) -> str:
    """Preserve a response tool's namespace without retaining its arguments."""
    name = str(payload.get("name") or "?")
    namespace = str(payload.get("namespace") or "").strip()
    if not namespace:
        return name
    return f"{namespace}{'' if namespace.endswith('__') else '__'}{name}"


def _parse_rollout(path: Path, fallback_model: str) -> dict:
    stat = path.stat()
    signature = (stat.st_size, stat.st_mtime_ns, fallback_model)
    cache_key = str(path)
    cached = _ROLLOUT_CACHE.get(cache_key)
    if cached and cached[0] == signature:
        _ROLLOUT_CACHE.move_to_end(cache_key)
        return cached[1]

    daily_models: dict[tuple[str, str], int] = defaultdict(int)
    daily_tools: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    tools: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    calls: dict[str, tuple[str, str]] = {}
    errored_calls: set[str] = set()
    task_ids: set[str] = set()
    previous = {"input": 0, "cached": 0, "output": 0}
    totals = {"fresh": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    current_model = fallback_model or "unknown"
    anonymous_call = 0
    anonymous_turn = 0
    token_snapshots = 0

    def record_call(call_id: Any, name: Any, day: str) -> str:
        nonlocal anonymous_call
        normalized_name = str(name or "?")
        normalized_id = str(call_id or "")
        if not normalized_id:
            anonymous_call += 1
            normalized_id = f"anonymous-{anonymous_call}"
        if normalized_id in calls:
            return normalized_id
        calls[normalized_id] = (normalized_name, day)
        tools[normalized_name][0] += 1
        if day:
            daily_tools[(day, normalized_name)][0] += 1
        return normalized_id

    def mark_error(call_id: Any) -> None:
        normalized_id = str(call_id or "")
        if not normalized_id or normalized_id in errored_calls:
            return
        call = calls.get(normalized_id)
        if not call:
            return
        errored_calls.add(normalized_id)
        name, day = call
        tools[name][1] += 1
        if day:
            daily_tools[(day, name)][1] += 1

    with path.open("rb") as handle:
        for raw_line in handle:
            if not any(marker in raw_line for marker in _INTERESTING):
                continue
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Active rollouts can end in a partial line while the API is
                # reading. Earlier complete observations remain trustworthy.
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            event_type = event.get("type")
            payload_type = payload.get("type")
            event_day = _day(event.get("timestamp"))

            if event_type == "turn_context":
                if isinstance(payload.get("model"), str) and payload["model"]:
                    current_model = payload["model"]
                continue

            if event_type == "event_msg" and payload_type == "task_started":
                turn_id = str(payload.get("turn_id") or "")
                if not turn_id:
                    anonymous_turn += 1
                    turn_id = f"anonymous-{anonymous_turn}"
                task_ids.add(turn_id)
                continue

            if event_type == "event_msg" and payload_type == "token_count":
                snapshot = _usage_snapshot(payload)
                if snapshot is None:
                    continue
                token_snapshots += 1
                delta: dict[str, int] = {}
                for key in previous:
                    value = snapshot[key]
                    delta[key] = value - previous[key] if value >= previous[key] else value
                previous = snapshot
                fresh = max(0, delta["input"] - delta["cached"])
                totals["fresh"] += fresh
                totals["cache_read"] += delta["cached"]
                totals["output"] += delta["output"]
                token_delta = fresh + delta["cached"] + delta["output"]
                if event_day and token_delta:
                    daily_models[(event_day, current_model or "unknown")] += token_delta
                continue

            if event_type == "response_item" and payload_type in {
                "function_call", "custom_tool_call"
            }:
                record_call(
                    payload.get("call_id") or payload.get("id"),
                    _response_tool_name(payload),
                    event_day,
                )
                continue

            if event_type != "event_msg":
                continue

            call_id = payload.get("call_id")
            if payload_type == "mcp_tool_call_end":
                invocation = payload.get("invocation")
                invocation = invocation if isinstance(invocation, dict) else {}
                server = str(invocation.get("server") or "unknown")
                tool = str(invocation.get("tool") or "unknown")
                call_id = record_call(
                    call_id, f"mcp__{server}__{tool}", event_day
                )
                result = payload.get("result")
                if isinstance(result, dict) and "Err" in result:
                    mark_error(call_id)
            elif payload_type == "exec_command_end":
                call_id = record_call(call_id, "exec", event_day)
                if payload.get("exit_code") not in (None, 0):
                    mark_error(call_id)
            elif payload_type == "patch_apply_end":
                call_id = record_call(call_id, "apply_patch", event_day)
                if payload.get("success") is False:
                    mark_error(call_id)
            elif payload_type == "web_search_end":
                record_call(call_id, "web_search", event_day)
            elif payload_type == "view_image_tool_call":
                record_call(call_id, "view_image", event_day)
            elif payload_type == "image_generation_end":
                record_call(call_id, "image_generation", event_day)

    result = {
        **totals,
        "tokens": sum(totals.values()),
        "token_snapshots": token_snapshots,
        "turns": len(task_ids),
        "daily_models": [
            {"day": day, "model": model, "tokens": tokens}
            for (day, model), tokens in sorted(daily_models.items())
        ],
        "daily_tools": [
            {"day": day, "name": name, "calls": values[0], "errors": values[1]}
            for (day, name), values in sorted(daily_tools.items())
        ],
        "tools": [
            {"name": name, "calls": values[0], "errors": values[1]}
            for name, values in sorted(
                tools.items(), key=lambda item: (-item[1][0], item[0])
            )
        ],
    }
    _ROLLOUT_CACHE[cache_key] = (signature, result)
    _ROLLOUT_CACHE.move_to_end(cache_key)
    while len(_ROLLOUT_CACHE) > MAX_ROLLOUT_CACHE:
        _ROLLOUT_CACHE.popitem(last=False)
    return result


def _empty_rollout() -> dict:
    """A content-free detail shell for unavailable or intentionally skipped logs."""
    return {
        "fresh": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "tokens": 0,
        "token_snapshots": 0,
        "turns": 0,
        "daily_models": [],
        "daily_tools": [],
        "tools": [],
    }


def _reconcile_usage(
    parsed: dict,
    authoritative_tokens: int,
    fallback_day: str,
    fallback_model: str,
) -> dict:
    """Reconcile rollout detail to the state DB's authoritative token total.

    Parsed token classes remain useful when they are a prefix of the state
    total. Any gap is explicit ``unclassified`` usage; it is never mislabeled
    as fresh input. If a rollout is ahead of the state snapshot, its newer
    cumulative total and classified breakdown win for that refresh.
    """
    state_total = max(0, int(authoritative_tokens or 0))
    parsed_total = max(0, int(parsed.get("tokens") or 0))
    # A rollout may advance just after the state snapshot was read. Keep the
    # newest trustworthy observation instead of downgrading fresh detail.
    total = max(state_total, parsed_total)
    has_snapshot = int(parsed.get("token_snapshots") or 0) > 0
    result = dict(parsed)

    if has_snapshot:
        result["tokens"] = total
        result["unclassified"] = total - parsed_total
        result["breakdown_known"] = parsed_total == total
        daily_rows = [
            {**row, "unclassified": int(row.get("unclassified") or 0)}
            for row in parsed.get("daily_models") or []
        ]
        daily_total = sum(
            max(0, int(row.get("tokens") or 0)) for row in daily_rows
        )
        missing_daily = max(0, total - daily_total)
        if missing_daily and fallback_day:
            daily_rows.append({
                "day": fallback_day,
                "model": fallback_model or "unknown",
                "tokens": missing_daily,
                "unclassified": missing_daily,
            })
        result["daily_models"] = daily_rows
        return result

    # No recognized token snapshot. Preserve non-token detail but do not claim
    # a token class for the state DB total.
    result.update({
        "fresh": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "tokens": total,
        "unclassified": total,
        "breakdown_known": False,
        "daily_models": ([{
            "day": fallback_day,
            "model": fallback_model or "unknown",
            "tokens": total,
            "unclassified": total,
        }] if total and fallback_day else []),
    })
    return result


def _safe_rollout(root: Path, raw_path: str) -> Path | None:
    if not raw_path:
        return None
    sessions_root = (root / "sessions").resolve()
    candidate = Path(raw_path).expanduser().resolve()
    if candidate != sessions_root and sessions_root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _root_for(session_id: str, parent_of: dict[str, str], known: set[str]) -> str:
    current = session_id
    seen = {current}
    while current in parent_of and parent_of[current] in known:
        parent = parent_of[current]
        if parent in seen:
            break
        seen.add(parent)
        current = parent
    return current


def collect_session_telemetry() -> dict:
    root = _codex_home()
    state_path = _find_state_db(root)
    state_rows, parent_of, schema_version = _load_state(state_path)
    if not state_rows:
        raise ValueError(f"Codex state DB has no sessions: {state_path}")

    rows_by_id = {row["id"]: row for row in state_rows}
    known_ids = set(rows_by_id)
    root_for = {
        session_id: _root_for(session_id, parent_of, known_ids)
        for session_id in known_ids
    }
    children: dict[str, list[str]] = defaultdict(list)
    for session_id, root_id in root_for.items():
        if session_id != root_id:
            children[root_id].append(session_id)

    tokens_by_root: dict[str, int] = defaultdict(int)
    for row in state_rows:
        tokens_by_root[root_for[row["id"]]] += max(0, row["tokens_used"])

    parsed_by_id: dict[str, dict] = {}
    invalid_rollouts = 0
    skipped_for_budget = 0
    detailed_rollouts = 0
    detailed_bytes = 0
    zero_probe_rollouts = 0
    zero_probe_bytes = 0
    zero_probe_skipped_for_budget = 0

    def load_detail(row: dict, *, recovery_probe: bool = False) -> dict:
        """Read one safe rollout within its bounded detail budget.

        Zero-state recovery probes use a small, independent side budget so a
        backlog of tokenless roots can never reduce the capacity reserved for
        ordinary token-bearing session detail.
        """
        nonlocal invalid_rollouts, skipped_for_budget
        nonlocal detailed_rollouts, detailed_bytes
        nonlocal zero_probe_rollouts, zero_probe_bytes
        nonlocal zero_probe_skipped_for_budget

        ended = _epoch_seconds(row["updated_at"], row["updated_at_ms"])
        fallback_day = (_iso(ended) or "")[:10]
        raw_detail = _empty_rollout()
        rollout = _safe_rollout(root, row["rollout_path"])
        if rollout is None:
            invalid_rollouts += 1
        else:
            try:
                rollout_size = rollout.stat().st_size
            except OSError:
                invalid_rollouts += 1
            else:
                if recovery_probe:
                    over_budget = (
                        zero_probe_rollouts >= MAX_ZERO_TOKEN_ROOT_PROBES
                        or zero_probe_bytes + rollout_size
                        > MAX_ZERO_TOKEN_PROBE_BYTES
                    )
                else:
                    over_budget = (
                        detailed_rollouts >= MAX_DETAILED_ROLLOUTS
                        or detailed_bytes + rollout_size > MAX_DETAILED_BYTES
                    )
                if over_budget:
                    if recovery_probe:
                        zero_probe_skipped_for_budget += 1
                    else:
                        skipped_for_budget += 1
                else:
                    try:
                        raw_detail = _parse_rollout(rollout, row["model"])
                    except OSError:
                        invalid_rollouts += 1
                    else:
                        if recovery_probe:
                            zero_probe_rollouts += 1
                            zero_probe_bytes += rollout_size
                        else:
                            detailed_rollouts += 1
                            detailed_bytes += rollout_size
        return _reconcile_usage(
            raw_detail,
            row["tokens_used"],
            fallback_day,
            row["model"] or "unknown",
        )

    state_meaningful_roots = {
        root_id for root_id, tokens in tokens_by_root.items() if tokens > 0
    }

    # State and rollout writes are not atomic together. Probe only a small,
    # recent root window so a just-started session whose rollout is ahead of a
    # zero state total can appear without scanning unbounded tokenless history.
    zero_root_candidates = sorted(
        (
            row for row in state_rows
            if root_for[row["id"]] == row["id"]
            and tokens_by_root[row["id"]] == 0
        ),
        key=lambda row: _epoch_seconds(row["updated_at"], row["updated_at_ms"]),
        reverse=True,
    )[:MAX_ZERO_TOKEN_ROOT_PROBES]
    recovered_zero_roots: set[str] = set()
    for row in zero_root_candidates:
        parsed = load_detail(row, recovery_probe=True)
        if parsed["tokens"] > 0:
            parsed_by_id[row["id"]] = parsed
            recovered_zero_roots.add(row["id"])

    meaningful_roots = state_meaningful_roots | recovered_zero_roots

    # Keep an orchestration-only parent when any descendant did useful work.
    # Also retain a bounded recent root recovered from a rollout that advanced
    # just before its state row. Other tokenless roots remain omitted.
    parent_rows = [
        row for row in state_rows
        if root_for[row["id"]] == row["id"]
        and row["id"] in meaningful_roots
    ]
    if not parent_rows:
        raise ValueError(f"Codex state DB has no token-bearing sessions: {state_path}")
    parent_rows.sort(
        key=lambda row: _epoch_seconds(row["created_at"], row["created_at_ms"]),
        reverse=True,
    )
    kept = parent_rows[:MAX_SESSIONS]
    kept_ids = {row["id"] for row in kept}

    # Scan detail for the newest visible roots first, then remaining meaningful
    # history. Reconciled state/rollout totals remain exact even when the
    # bounded detail budget is exhausted.
    def detail_priority(row: dict) -> tuple[int, float, int, float]:
        root_id = root_for[row["id"]]
        root_row = rows_by_id[root_id]
        return (
            int(root_id in kept_ids),
            _epoch_seconds(root_row["created_at"], root_row["created_at_ms"]),
            int(row["id"] == root_id),
            _epoch_seconds(row["updated_at"], row["updated_at_ms"]),
        )

    detail_rows = sorted(
        (row for row in state_rows if root_for[row["id"]] in meaningful_roots),
        key=detail_priority,
        reverse=True,
    )
    for row in detail_rows:
        if row["id"] not in parsed_by_id:
            parsed_by_id[row["id"]] = load_detail(row)

    # Non-meaningful zero-token rows are not scanned, but keeping an explicit
    # detail shell simplifies safe descendant lookups and preserves invariants.
    for row in state_rows:
        if row["id"] not in parsed_by_id:
            parsed_by_id[row["id"]] = _reconcile_usage(
                _empty_rollout(), row["tokens_used"], "",
                row["model"] or "unknown",
            )

    daily_models: dict[tuple[str, str], list[int]] = defaultdict(
        lambda: [0, 0]
    )
    daily_sessions: dict[tuple[str, str], list[int]] = defaultdict(
        lambda: [0, 0]
    )
    daily_tools: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])

    for row in state_rows:
        parsed = parsed_by_id[row["id"]]
        for model_row in parsed["daily_models"]:
            day = model_row["day"]
            tokens = int(model_row["tokens"] or 0)
            unclassified = int(model_row.get("unclassified") or 0)
            model_values = daily_models[(day, model_row["model"] or "unknown")]
            model_values[0] += tokens
            model_values[1] += unclassified
            session_values = daily_sessions[(day, root_for[row["id"]])]
            session_values[0] += tokens
            session_values[1] += unclassified
        for tool_row in parsed["daily_tools"]:
            values = daily_tools[(tool_row["day"], tool_row["name"])]
            values[0] += int(tool_row["calls"] or 0)
            values[1] += int(tool_row["errors"] or 0)

    sessions = []
    for row in kept:
        parsed = parsed_by_id[row["id"]]
        started = _epoch_seconds(row["created_at"], row["created_at_ms"])
        ended = _epoch_seconds(row["updated_at"], row["updated_at_ms"])
        project_path = row["cwd"]
        descendants = sorted(
            children.get(row["id"], []),
            key=lambda child_id: _epoch_seconds(
                rows_by_id[child_id]["created_at"],
                rows_by_id[child_id]["created_at_ms"],
            ),
        )
        tree_details = [parsed, *(parsed_by_id[child_id] for child_id in descendants)]
        tree_tokens = sum(detail["tokens"] for detail in tree_details)
        tree_fresh = sum(detail["fresh"] for detail in tree_details)
        tree_output = sum(detail["output"] for detail in tree_details)
        tree_cache_read = sum(detail["cache_read"] for detail in tree_details)
        tree_cache_write = sum(detail["cache_write"] for detail in tree_details)
        tree_unclassified = sum(detail["unclassified"] for detail in tree_details)
        sessions.append({
            "id": row["id"],
            "key": f"{SOURCE_ID}:{row['id']}",
            "agent": SOURCE_ID,
            "project": (
                project_path.rstrip("/").rsplit("/", 1)[-1]
                if project_path else "(unknown)"
            ),
            "project_path": project_path,
            "started_at": _iso(started),
            "ended_at": _iso(ended),
            "duration_sec": max(0, int(ended - started)) if started and ended else None,
            "model": row["model"],
            "agent_version": row["cli_version"],
            "turns": parsed["turns"],
            "fresh": tree_fresh,
            "output": tree_output,
            "cache_read": tree_cache_read,
            "cache_write": tree_cache_write,
            "tokens": tree_tokens,
            "own_tokens": parsed["tokens"],
            "total_tokens": tree_tokens,
            "unclassified": tree_unclassified,
            "breakdown_known": all(
                detail["breakdown_known"] for detail in tree_details
            ),
            "cost": 0.0,
            "cost_known": False,
            "tools": parsed["tools"][:MAX_TOOLS_PER_SESSION],
            "subagents": [
                {
                    "id": child_id,
                    "tokens": parsed_by_id[child_id]["tokens"],
                    "own_tokens": parsed_by_id[child_id]["tokens"],
                    "total_tokens": parsed_by_id[child_id]["tokens"],
                    "unclassified": parsed_by_id[child_id]["unclassified"],
                    "breakdown_known": parsed_by_id[child_id]["breakdown_known"],
                    "cost": 0.0,
                    "cost_known": False,
                    "turns": parsed_by_id[child_id]["turns"],
                }
                for child_id in descendants
            ],
        })

    projects = {row["cwd"] or "(unknown)" for row in parent_rows}
    all_tokens = sum(parsed_by_id[row["id"]]["tokens"] for row in state_rows)
    source = {
        "id": SOURCE_ID,
        "label": SOURCE_LABEL,
        "cost_status": COST_STATUS,
        "invalid_rollouts": invalid_rollouts,
        "rollouts_skipped_for_budget": skipped_for_budget,
        "detailed_rollouts": detailed_rollouts,
        "detailed_bytes": detailed_bytes,
        "zero_token_roots_considered": len(zero_root_candidates),
        "zero_token_roots_probed": zero_probe_rollouts,
        "zero_token_probe_bytes": zero_probe_bytes,
        "zero_token_roots_skipped_for_probe_budget": (
            zero_probe_skipped_for_budget
        ),
        "zero_token_roots_recovered": len(recovered_zero_roots),
    }
    return {
        "source": source,
        "meta_priority": META_PRIORITY,
        "meta": {
            "db_path": str(state_path),
            "schema_version": schema_version,
            "pricing_version": None,
        },
        "totals": {
            "sessions": len(parent_rows),
            "tokens": all_tokens,
            "cost_usd": 0.0,
            "projects": len(projects),
            "sessions_by_agent": {SOURCE_ID: len(parent_rows)},
        },
        "project_keys": sorted(projects),
        "sessions": sessions,
        "daily_models": [
            {
                "day": day, "agent": SOURCE_ID, "model": model,
                "tokens": values[0], "unclassified": values[1],
                "breakdown_known": values[1] == 0,
                "cost": 0.0, "cost_known": False,
            }
            for (day, model), values in sorted(daily_models.items())
        ],
        "daily_sessions": [
            {
                "day": day, "agent": SOURCE_ID, "session_id": session_id,
                "session_key": f"{SOURCE_ID}:{session_id}",
                "tokens": values[0], "unclassified": values[1],
                "breakdown_known": values[1] == 0,
                "cost": 0.0, "cost_known": False,
            }
            for (day, session_id), values in sorted(daily_sessions.items())
            if session_id in kept_ids
        ],
        "daily_tools": [
            {
                "day": day, "agent": SOURCE_ID, "name": name,
                "calls": values[0], "errors": values[1],
            }
            for (day, name), values in sorted(daily_tools.items())
        ],
    }
