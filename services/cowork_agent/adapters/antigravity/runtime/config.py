"""How XO runs agy, from ``config/agents/antigravity/settings.json``:

- ``transport``: ``acp`` (the default) or ``print``. ACP still needs agy
  1.5.15 or newer; an older agy, or a spec only print mode serves, runs on
  print.
- ``terminal_sandbox``: ``auto`` (the default: XO checks once whether agy's
  terminal sandbox can start here), ``on`` or ``off``.

A missing or unknown value is the default, and an unknown one is logged.
"""

from __future__ import annotations

import logging

from services.cowork_agent.registry.settings import load_agent_config

AGENT = "antigravity"
TRANSPORTS = ("acp", "print")
SANDBOX = ("auto", "on", "off")
log = logging.getLogger(__name__)


def load() -> dict:
    try:
        return load_agent_config(AGENT)
    except (OSError, ValueError) as exc:
        log.warning("agy settings unreadable, using the defaults: %s", exc)
        return {}


def _choice(key: str, allowed: tuple[str, ...]) -> str:
    raw = load().get(key)
    value = raw.strip().lower() if isinstance(raw, str) else ""
    if value in allowed:
        return value
    if raw is not None:
        log.warning("agy setting %s=%r isn't one of %s; using %s", key, raw, "/".join(allowed), allowed[0])
    return allowed[0]


def transport() -> str:
    return _choice("transport", TRANSPORTS)


def terminal_sandbox() -> str:
    return _choice("terminal_sandbox", SANDBOX)


__all__ = ["SANDBOX", "TRANSPORTS", "load", "terminal_sandbox", "transport"]
