"""
Usage of every runtime on this machine, for ``/api/usage`` and the daily sync.

The Space's usage is the usage of every runtime it runs, not only of the
Space's agent: a Space whose threads run on two runtimes spends tokens in
both. Each runtime reads its own numbers in its capability module
``services.cowork_agent.adapters.<name>.usage`` (discovered through
:func:`~services.cowork_agent.adapters.loader.list_capability_providers`, so
adding a runtime is still "drop adapters/<name>/usage.py", with no core edit).
:func:`usage_providers` yields every such module that imports.
:meth:`AllRuntimesUsage.dashboard` merges their dashboards (totals summed,
per-day and per-model rows sharing a key collapse into one, session rows
labelled with an added ``runtime`` field), and
:meth:`AllRuntimesUsage.sync_payload` adds up their days for the daily sync.

A runtime counts when its module loads and reports data. One that has none
(not used on this machine) contributes nothing, so a single-runtime Space
answers exactly what its runtime answers, plus the label. Deciding this never
starts a CLI: the modules only read the files and databases the CLIs leave.

A module that fails to import, or raises while answering the dashboard, is
logged and skipped so one broken runtime cannot blank the others' usage. Only
when every runtime fails is the first failure raised, as a single module's
failure was before. The daily sync's :func:`all_runtimes_sync_payload` is the
exception: any runtime that fails fails it, since a day sent without a share
is short.

:func:`load_usage_module` (the Space's agent's module alone) answers the
other usage views.
"""
from __future__ import annotations

import copy
import logging
from types import ModuleType
from typing import Any, Callable, Iterable, Optional

from services.cowork_agent.adapters.loader import (
    list_capability_providers,
    load_capability,
    try_load_capability,
)
from services.cowork_agent.registry.agent_registry import get_active_agent

logger = logging.getLogger(__name__)

CAPABILITY = "usage"
#: The field added to every session row of a merged answer: the runtime it came from.
RUNTIME_FIELD = "runtime"
#: A /usage/report day's cost fields, rounded after they are added up.
COST_FIELDS = ("total_cost", "input_cost", "output_cost", "cache_read_cost", "cache_write_cost")

# Last logged reason per broken provider. The usage routes run on every page
# load, so a runtime whose module cannot import would otherwise repeat the
# same warning for the life of the process; a changed reason logs again.
_broken: dict[str, str] = {}


def load_usage_module() -> ModuleType:
    """Return the active agent's usage module.

    Thin alias over :func:`services.cowork_agent.adapters.loader.load_capability`
    (``capability="usage"``), for callers that want the Space's agent alone.

    Raises:
        ModuleNotFoundError: if the active agent has no
            ``services/cowork_agent/adapters/<name>/usage.py``. The error
            names the path we tried to import so the fix is obvious.
    """
    return load_capability(CAPABILITY)


def _note_broken(name: str, reason: str) -> None:
    if _broken.get(name) == reason:
        return
    _broken[name] = reason
    logger.warning("usage: skipping %s, its usage module failed: %s", name, reason)


def _space_agent_name() -> Optional[str]:
    """The Space's agent, listed first so its rows lead and its answer wins ties."""
    try:
        return get_active_agent().name
    except Exception:  # noqa: BLE001 - no resolvable agent only loses the ordering
        return None


def usage_providers() -> list[tuple[str, ModuleType]]:
    """Every runtime whose usage module imports, as ``(name, module)``.

    The Space's agent comes first, then the rest by name. A module that fails
    to import is logged once and skipped; a runtime without one is simply
    absent.
    """
    first = _space_agent_name()
    providers: list[tuple[str, ModuleType]] = []
    for name in sorted(list_capability_providers(CAPABILITY), key=lambda n: (n != first, n)):
        try:
            module = try_load_capability(CAPABILITY, agent=name)
        except Exception as exc:  # noqa: BLE001 - one broken runtime must not hide the others
            _note_broken(name, f"{type(exc).__name__}: {exc}")
            continue
        _broken.pop(name, None)
        if module is not None:
            providers.append((name, module))
    return providers


def all_runtimes_usage() -> "AllRuntimesUsage":
    """The merged usage of every runtime.

    Raises:
        ModuleNotFoundError: when no runtime has a usage module that loads,
            so the route answers 501 as it did for an agent without one.
    """
    providers = usage_providers()
    if not providers:
        raise ModuleNotFoundError(
            "no runtime has a usage module that loads "
            "(tried services.cowork_agent.adapters.<name>.usage for every adapter)"
        )
    return AllRuntimesUsage(providers)


# ─────────────────────────────────────────────────────────────────────────────
# Merge helpers. Every helper copies; a provider's answer is never mutated.
# ─────────────────────────────────────────────────────────────────────────────


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _add(have: Any, value: Any) -> Any:
    """Sum two values of one field: numbers add, dicts add per key, anything else keeps the first."""
    if _number(have) and _number(value):
        return have + value
    if isinstance(have, dict) and isinstance(value, dict):
        out = dict(have)
        for key, item in value.items():
            out[key] = _add(out[key], item) if key in out else copy.deepcopy(item)
        return out
    return have


def _merge_rows(lists: Iterable[Optional[list]], key: tuple[str, ...]) -> list[dict]:
    """Rows sharing ``key`` collapse into one whose numbers are summed; first-seen order."""
    merged: dict[tuple, dict] = {}
    for rows in lists:
        for row in rows or []:
            k = tuple(row.get(field) for field in key)
            have = merged.get(k)
            if have is None:
                merged[k] = copy.deepcopy(row)
                continue
            for field, value in row.items():
                if field not in key:
                    have[field] = _add(have.get(field), value) if field in have else copy.deepcopy(value)
    return list(merged.values())


def _rounded(rows: list[dict], *fields: str, digits: int = 6) -> list[dict]:
    """Round summed costs: adding rounded floats leaves binary tails (0.1 + 0.2)."""
    for row in rows:
        for field in fields:
            if _number(row.get(field)) and isinstance(row[field], float):
                row[field] = round(row[field], digits)
    return rows


def _by_date(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: r.get("date") or "")


def _weighted(parts: list[tuple[float, float]]) -> float:
    total = sum(weight for _value, weight in parts)
    return sum(value * weight for value, weight in parts) / total if total else 0


def _labelled(rows: Optional[list], name: str) -> list:
    return [{**row, RUNTIME_FIELD: name} if isinstance(row, dict) else row for row in rows or []]


# ─────────────────────────────────────────────────────────────────────────────
# The merged usage
# ─────────────────────────────────────────────────────────────────────────────


class AllRuntimesUsage:
    """Every runtime's usage at once: the dashboard merged, and the sync's days added up.

    With no data anywhere the dashboard answers what the first provider (the
    Space's agent, when it has a usage module) answers, so an empty Space keeps
    today's empty shape. With data in one runtime it answers that runtime's
    payload unchanged but for the ``runtime`` label on session rows.
    """

    def __init__(self, providers: list[tuple[str, ModuleType]]) -> None:
        self.providers = providers

    def _each(self, view: str, *args: Any, **kwargs: Any) -> list[tuple[str, Any]]:
        """``(name, answer)`` for every provider that implements ``view`` and answered."""
        answers: list[tuple[str, Any]] = []
        first_error: Optional[BaseException] = None
        for name, module in self.providers:
            fn: Optional[Callable] = getattr(module, view, None)
            if not callable(fn):
                continue
            try:
                answers.append((name, fn(*args, **kwargs)))
            except Exception as exc:  # noqa: BLE001 - one failing runtime must not blank the others
                logger.warning("usage: %s.%s failed, leaving it out: %s", name, view, exc)
                first_error = first_error or exc
        if not answers and first_error is not None:
            raise first_error
        if not answers:
            raise AttributeError(f"no runtime's usage module implements {view}()")
        return answers

    @staticmethod
    def _pick(answers: list[tuple[str, Any]], has_data: Callable[[Any], bool]) -> list[tuple[str, Any]]:
        return [(name, answer) for name, answer in answers if isinstance(answer, dict) and has_data(answer)]

    def dashboard(self, *, window: dict) -> dict:
        answers = self._each("dashboard", window=window)
        parts = self._pick(answers, lambda p: bool(
            p.get("total_messages") or p.get("total_sessions") or p.get("total_cost")
            or any(_number(v) and v for v in (p.get("total_tokens") or {}).values())
        ))
        if not parts:
            return answers[0][1]
        if len(parts) == 1:
            name, only = parts[0]
            return {**only, "by_session": _labelled(only.get("by_session"), name)}
        payloads = [p for _name, p in parts]

        tokens: dict = {}
        for p in payloads:
            tokens = _add(tokens, p.get("total_tokens") or {})
        total_sessions = sum(p.get("total_sessions") or 0 for p in payloads)
        flat = sum(tokens.get(k, 0) or 0 for k in ("input", "output", "reasoning"))
        response_time = self._merge_response_time([p.get("response_time") or {} for p in payloads])
        by_model = _rounded(_merge_rows([p.get("by_model") for p in payloads], ("model_id", "provider_id")),
                            "total_cost")
        by_model.sort(key=lambda m: m.get("message_count") or 0, reverse=True)
        by_session = [row for name, p in parts for row in _labelled(p.get("by_session"), name)]
        by_session.sort(key=lambda s: s.get("total_tokens") or 0, reverse=True)

        merged = {
            "total_cost": round(sum(p.get("total_cost") or 0 for p in payloads), 6),
            "total_tokens": tokens,
            "total_sessions": total_sessions,
            "total_messages": sum(p.get("total_messages") or 0 for p in payloads),
            "avg_tokens_per_session": round(flat / total_sessions, 2) if total_sessions else 0,
            "avg_response_time": response_time["avg"],
            "by_model": by_model,
            "by_session": by_session[:10],
            "daily": _rounded(_by_date(_merge_rows([p.get("daily") for p in payloads], ("date",))), "cost"),
            "response_time": response_time,
        }
        if any(p.get("estimated_tokens") for p in payloads):
            merged["estimated_tokens"] = True          # some of the summed tokens are estimates
        return merged

    @staticmethod
    def _merge_response_time(stats: list[dict]) -> dict:
        """Seconds, ``{avg, median, p95, min, max, count}``. Median and p95 are
        count-weighted means of the runtimes' own (the union's cannot be rebuilt)."""
        seen = [s for s in stats if (s.get("count") or 0) > 0]
        if not seen:
            return {"avg": 0, "median": 0, "p95": 0, "min": 0, "max": 0, "count": 0}
        out = {key: round(_weighted([(s.get(key) or 0, s["count"]) for s in seen]), 3)
               for key in ("avg", "median", "p95")}
        out["min"] = round(min(s.get("min") or 0 for s in seen), 3)
        out["max"] = round(max(s.get("max") or 0 for s in seen), 3)
        out["count"] = sum(s["count"] for s in seen)
        return out

    def sync_payload(self, *, since_date: Optional[str] = None) -> dict:
        """Every runtime's ``sync_payload`` added up per day, in the shape one module answers:
        ``{date: day}`` with ``__session_dates__`` and ``__parse_errors__`` (each named by its runtime).

        Unlike the views, a runtime that raises fails the whole answer: xo-swarm replaces a day
        with each report, so a day without one runtime's share would go out short.
        """
        days: dict[str, dict] = {}
        sessions: dict[str, int] = {}
        errors: list[str] = []
        for name, module in self.providers:
            payload = dict(module.sync_payload(since_date=since_date) or {})
            for date, count in (payload.pop("__session_dates__", None) or {}).items():
                sessions[date] = sessions.get(date, 0) + count
            errors.extend(f"{name}: {error}" for error in payload.pop("__parse_errors__", None) or [])
            for date, day in payload.items():
                if str(date).startswith("__") or not isinstance(day, dict):
                    continue
                days[date] = _sum_day(days[date], day) if date in days else copy.deepcopy(day)
        out: dict = dict(sorted(days.items()))
        out["__session_dates__"] = sessions
        if errors:
            out["__parse_errors__"] = errors
        return out


def _sum_day(have: dict, day: dict) -> dict:
    """Two runtimes' records of one day (the /usage/report shape) added up."""
    rows = ("model_usage", "tool_usage")
    out = _add(have, {k: v for k, v in day.items() if k not in rows})
    out["model_usage"] = _rounded(_merge_rows([have.get("model_usage"), day.get("model_usage")],
                                              ("provider", "model")), "cost", digits=8)
    out["tool_usage"] = sorted(_merge_rows([have.get("tool_usage"), day.get("tool_usage")], ("name",)),
                               key=lambda t: -(t.get("count") or 0))
    return _rounded([out], *COST_FIELDS, digits=8)[0]


def all_runtimes_sync_payload(*, since_date: Optional[str] = None) -> dict:
    """:meth:`AllRuntimesUsage.sync_payload` over every runtime. Raises when any runtime's usage
    module doesn't load (its share would be missing) or none has one."""
    providers = usage_providers()
    if _broken:
        name, reason = sorted(_broken.items())[0]
        raise ImportError(f"{name}'s usage module doesn't load ({reason})")
    if not providers:
        raise ModuleNotFoundError("no runtime has a usage module that loads")
    return AllRuntimesUsage(providers).sync_payload(since_date=since_date)
