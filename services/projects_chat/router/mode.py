"""The router switches, read here and nowhere else.

``XO_ROUTER_MODE`` (default ``agent``):

- ``off``: no router. ``/api/chat`` runs exactly as it always has and the
  projects chat refuses new messages.
- ``agent``: the active backend, at low effort, is the coordinator; its comms
  tool calls are the routing decisions.
- ``decision``: a decision model (``XO_ROUTER_DECISION_MODEL``, ``sage`` or
  ``jev``) decides; when it is unsure the message goes to the agent router.

``XO_ROUTER_WORKSPACE`` and ``XO_ROUTER_PROJECT`` override the mode for one
level. Anything unrecognised reads as ``off``, with one warning, so a typo can
never start agents or network calls.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

OFF, AGENT, DECISION = "off", "agent", "decision"
MODES = (OFF, AGENT, DECISION)
LEVELS = ("workspace", "project")
DECISION_MODELS = ("sage", "jev")

ENV_MODE = "XO_ROUTER_MODE"
ENV_LEVEL = {"workspace": "XO_ROUTER_WORKSPACE", "project": "XO_ROUTER_PROJECT"}
ENV_MODEL = "XO_ROUTER_DECISION_MODEL"
ENV_ABSTAIN = "XO_ROUTER_ABSTAIN_BELOW"
ENV_MAX_TURNS = "XO_SESSIONS_MAX_TURNS"
ENV_THREAD_BUDGET = "XO_THREAD_BUDGET_USD"

_warned: set[str] = set()


def _warn_once(key: str, message: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def _read(name: str) -> str:
    return (os.getenv(name, "") or "").strip().lower()


def mode(level: str | None = None) -> str:
    """The mode for ``level`` (``workspace`` / ``project``), or the global one."""
    if level is not None and level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    if level is not None:
        raw = _read(ENV_LEVEL[level])
        if raw:
            if raw in MODES:
                return raw
            _warn_once(f"{level}:{raw}", "%s=%r is not one of %s; that level stays off",
                       ENV_LEVEL[level], raw, ", ".join(MODES))
            return OFF
    raw = _read(ENV_MODE)
    if not raw:
        return AGENT
    if raw in MODES:
        return raw
    _warn_once(raw, "%s=%r is not one of %s; the router stays off", ENV_MODE, raw, ", ".join(MODES))
    return OFF


def enabled() -> bool:
    """True when any level routes (the projects chat accepts messages)."""
    return any(mode(level) != OFF for level in LEVELS)


def decision_model() -> str:
    raw = _read(ENV_MODEL) or "sage"
    if raw in DECISION_MODELS:
        return raw
    _warn_once(f"model:{raw}", "%s=%r is not one of %s; using sage", ENV_MODEL, raw, ", ".join(DECISION_MODELS))
    return "sage"


def abstain_below() -> float:
    """Confidence under which a decision model's answer goes to the agent router."""
    try:
        value = float(_read(ENV_ABSTAIN) or 0.5)
    except ValueError:
        value = 0.5
    return min(1.0, max(0.0, value))


def max_turns() -> int:
    """How many agent turns may run at once across the Space (default 3)."""
    try:
        return max(1, int(_read(ENV_MAX_TURNS) or 3))
    except ValueError:
        return 3


def thread_budget_usd() -> float | None:
    """A per-turn spend cap for threads, or ``None`` (the default: no cap)."""
    raw = _read(ENV_THREAD_BUDGET)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        _warn_once(f"budget:{raw}", "%s=%r is not a number; no budget applies", ENV_THREAD_BUDGET, raw)
        return None
    return value if value > 0 else None
