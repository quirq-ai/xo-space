"""Aggregate every installed read-only session telemetry capability.

Unlike chat/session-message routes, Space is a host-level observability view:
it should show all locally available runtimes at once. Providers are discovered
by capability rather than named here, so adding another source remains a
drop-in adapter operation.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from services.cowork_agent.adapters.loader import (
    list_capability_providers,
    try_load_capability,
)


_CAPABILITY = "session_telemetry"


class SessionTelemetryUnavailable(RuntimeError):
    """Raised only when no discovered telemetry provider can be read."""


# Last logged reason per unavailable provider. The watcher rebuilds this view
# every few seconds, and a runtime that is simply not installed (no Codex
# state DB, no Cursor home) would otherwise repeat the identical warning for
# the life of the process. Log a reason once; a changed reason or a recovery
# is news and logs again. Setup → Native session sources shows the live
# state of every source, so the log does not need to.
_unavailable_reasons: dict[str, str] = {}


def _note_provider_unavailable(provider: str, reason: str) -> None:
    if _unavailable_reasons.get(provider) == reason:
        return
    _unavailable_reasons[provider] = reason
    print(
        f"⚠️ session telemetry provider {provider} unavailable ({reason}) "
        "— logged once until it changes; see Setup → Native session sources"
    )


def _note_provider_available(provider: str) -> None:
    if _unavailable_reasons.pop(provider, None) is not None:
        print(f"✅ session telemetry provider {provider} available again")


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _source_shell(provider: str, module: Any | None = None) -> dict:
    source_id = getattr(module, "SOURCE_ID", provider)
    label = getattr(module, "SOURCE_LABEL", provider.replace("_", " ").title())
    return {"id": str(source_id), "label": str(label)}


def _non_negative_number(
    provider: str,
    path: str,
    value: Any,
    *,
    default: int | float = 0,
) -> int | float:
    """Return a JSON-safe telemetry number or reject the provider payload."""
    item = default if value is None else value
    if (
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(float(item))
        or item < 0
    ):
        raise TypeError(f"{provider} telemetry {path} must be a finite non-negative number")
    return item


def _normalize_row_numbers(provider: str, field: str, index: int, row: dict) -> None:
    """Validate numeric row contracts while filling legacy-compatible defaults."""
    prefix = f"{field}[{index}]"
    if field == "sessions":
        for key in (
            "turns", "fresh", "output", "cache_read", "cache_write",
            "unclassified", "cost",
        ):
            row[key] = _non_negative_number(
                provider, f"{prefix}.{key}", row.get(key)
            )
        if row.get("duration_sec") is not None:
            row["duration_sec"] = _non_negative_number(
                provider, f"{prefix}.duration_sec", row["duration_sec"]
            )
        if row.get("tokens") is None:
            row["tokens"] = sum(
                row[key]
                for key in (
                    "fresh", "output", "cache_read", "cache_write", "unclassified"
                )
            )
        else:
            row["tokens"] = _non_negative_number(
                provider, f"{prefix}.tokens", row["tokens"]
            )
        for key in ("own_tokens", "total_tokens"):
            if row.get(key) is None:
                row[key] = row["tokens"]
            else:
                row[key] = _non_negative_number(
                    provider, f"{prefix}.{key}", row[key]
                )

        nested_contracts = {
            "tools": ("calls", "errors"),
            "subagents": ("tokens", "unclassified", "cost", "turns"),
        }
        for nested_field, numeric_fields in nested_contracts.items():
            nested_rows = row.get(nested_field) or []
            if not isinstance(nested_rows, list) or not all(
                isinstance(item, dict) for item in nested_rows
            ):
                raise TypeError(
                    f"{provider} telemetry {prefix}.{nested_field} "
                    "must be an object list"
                )
            copied_nested = [dict(item) for item in nested_rows]
            for nested_index, item in enumerate(copied_nested):
                for key in numeric_fields:
                    item[key] = _non_negative_number(
                        provider,
                        f"{prefix}.{nested_field}[{nested_index}].{key}",
                        item.get(key),
                    )
                if nested_field == "subagents":
                    for key in ("own_tokens", "total_tokens"):
                        if item.get(key) is None:
                            item[key] = item["tokens"]
                        else:
                            item[key] = _non_negative_number(
                                provider,
                                f"{prefix}.{nested_field}[{nested_index}].{key}",
                                item[key],
                            )
            row[nested_field] = copied_nested
        return

    numeric_fields = {
        "daily_models": ("tokens", "unclassified", "cost"),
        "daily_sessions": ("tokens", "unclassified", "cost"),
        "daily_tools": ("calls", "errors"),
    }[field]
    for key in numeric_fields:
        row[key] = _non_negative_number(
            provider, f"{prefix}.{key}", row.get(key)
        )


def _validate_contribution(provider: str, module: Any, value: Any) -> dict:
    """Copy and validate one provider payload before it enters the merge.

    Providers are optional plugins. Keeping all contract checks inside their
    isolated collection block ensures one malformed payload cannot make healthy
    providers fail later during sorting or JSON serialization.
    """
    if not isinstance(value, dict):
        raise TypeError(f"{provider} telemetry collector returned non-object")

    contribution = dict(value)

    source_value = contribution.get("source") or {}
    if not isinstance(source_value, dict):
        raise TypeError(f"{provider} telemetry source metadata must be an object")
    source = _source_shell(provider, module)
    source.update(source_value)
    if not isinstance(source.get("id"), str) or not source["id"].strip():
        raise TypeError(f"{provider} telemetry source id must be a non-empty string")
    if not isinstance(source.get("label"), str) or not source["label"].strip():
        raise TypeError(f"{provider} telemetry source label must be a non-empty string")
    contribution["source"] = source

    meta = contribution.get("meta") or {}
    if not isinstance(meta, dict):
        raise TypeError(f"{provider} telemetry meta must be an object")
    contribution["meta"] = dict(meta)

    totals = contribution.get("totals") or {}
    if not isinstance(totals, dict):
        raise TypeError(f"{provider} telemetry totals must be an object")
    totals = dict(totals)
    for key in ("sessions", "tokens", "cost_usd"):
        item = totals.get(key, 0)
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or item < 0
        ):
            raise TypeError(
                f"{provider} telemetry total {key!r} must be a finite non-negative number"
            )
    contribution["totals"] = totals

    priority = contribution.get("meta_priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, (int, float)):
        raise TypeError(f"{provider} telemetry meta_priority must be numeric")
    contribution["meta_priority"] = int(priority)

    project_keys = contribution.get("project_keys") or []
    if not isinstance(project_keys, list) or not all(
        isinstance(key, str) for key in project_keys
    ):
        raise TypeError(f"{provider} telemetry project_keys must be a string list")
    contribution["project_keys"] = list(project_keys)

    sort_fields = {
        "sessions": ("started_at",),
        "daily_models": ("day", "agent", "model"),
        "daily_sessions": ("day", "agent", "session_id"),
        "daily_tools": ("day", "agent", "name"),
    }
    for field, string_fields in sort_fields.items():
        rows = contribution.get(field) or []
        if not isinstance(rows, list) or not all(
            isinstance(row, dict) for row in rows
        ):
            raise TypeError(f"{provider} telemetry {field} must be an object list")
        copied_rows = [dict(row) for row in rows]
        for index, row in enumerate(copied_rows):
            for key in string_fields:
                item = row.get(key)
                if item is not None and not isinstance(item, str):
                    raise TypeError(
                        f"{provider} telemetry {field}[{index}].{key} "
                        "must be a string or null"
                    )
            _normalize_row_numbers(provider, field, index, row)
        contribution[field] = copied_rows

    # Catch non-JSON values and NaN/Infinity while the provider is still inside
    # its failure boundary. FastAPI's JSONResponse would otherwise fail after
    # all healthy contributions had already been merged.
    json.dumps(contribution, allow_nan=False)
    return contribution


def build_session_telemetry() -> dict:
    contributions: list[dict] = []
    source_status: list[dict] = []

    for provider in list_capability_providers(_CAPABILITY):
        module = None
        try:
            module = try_load_capability(_CAPABILITY, agent=provider)
            if module is None:
                continue
            collect = getattr(module, "collect_session_telemetry", None)
            if not callable(collect):
                raise TypeError(
                    f"{provider} {_CAPABILITY} capability has no collector"
                )
            contribution = _validate_contribution(provider, module, collect())
            source = contribution["source"]
            totals = contribution.get("totals") or {}
            source.update({
                "status": "available",
                "available": True,
                "session_count": int(_number(totals.get("sessions"))),
                "token_count": int(_number(totals.get("tokens"))),
            })
            contribution["source"] = source
            contributions.append(contribution)
            source_status.append(source)
            _note_provider_available(provider)
        except Exception as exc:
            _note_provider_unavailable(provider, str(exc))
            source = _source_shell(provider, module)
            source.update({
                "status": "unavailable",
                "available": False,
                "cost_status": str(getattr(module, "COST_STATUS", "unknown")),
                "message": "Telemetry source unavailable.",
            })
            source_status.append(source)

    if not contributions:
        raise SessionTelemetryUnavailable("No session telemetry source is available.")

    primary = max(
        contributions,
        key=lambda item: int(item.get("meta_priority") or 0),
    )
    meta = dict(primary.get("meta") or {})
    meta["generated_at"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    meta["sources"] = source_status

    sessions: list[dict] = []
    daily_models: list[dict] = []
    daily_sessions: list[dict] = []
    daily_tools: list[dict] = []
    project_keys: set[str] = set()
    sessions_by_agent: dict[str, int] = {}
    tokens_by_agent: dict[str, int] = {}
    cost_by_agent: dict[str, float] = {}

    for contribution in contributions:
        source = contribution["source"]
        source_id = str(source["id"])
        totals = contribution.get("totals") or {}
        sessions_by_agent[source_id] = int(_number(totals.get("sessions")))
        tokens_by_agent[source_id] = int(_number(totals.get("tokens")))
        cost_by_agent[source_id] = _number(totals.get("cost_usd"))
        sessions.extend(contribution.get("sessions") or [])
        daily_models.extend(contribution.get("daily_models") or [])
        daily_sessions.extend(contribution.get("daily_sessions") or [])
        daily_tools.extend(contribution.get("daily_tools") or [])
        project_keys.update(str(key) for key in contribution.get("project_keys") or [])

    sessions.sort(key=lambda row: row.get("started_at") or "", reverse=True)
    daily_models.sort(
        key=lambda row: (row.get("day") or "", row.get("agent") or "",
                         row.get("model") or "")
    )
    daily_sessions.sort(
        key=lambda row: (row.get("day") or "", row.get("agent") or "",
                         row.get("session_id") or "")
    )
    daily_tools.sort(
        key=lambda row: (row.get("day") or "", row.get("agent") or "",
                         row.get("name") or "")
    )

    costs_complete = all(
        source.get("cost_status") != "unavailable"
        for source in source_status
        if source.get("available")
    )
    return {
        "meta": meta,
        "totals": {
            "sessions": sum(sessions_by_agent.values()),
            "tokens": sum(tokens_by_agent.values()),
            "cost_usd": sum(cost_by_agent.values()),
            "projects": len(project_keys),
            "sessions_by_agent": sessions_by_agent,
            "tokens_by_agent": tokens_by_agent,
            "cost_by_agent": cost_by_agent,
            "cost_complete": costs_complete,
        },
        "daily_models": daily_models,
        "daily_sessions": daily_sessions,
        "sessions": sessions,
        "daily_tools": daily_tools,
    }
