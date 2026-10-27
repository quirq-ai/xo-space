"""Reading a tool call's arguments: each check refuses with one plain
sentence the agent can act on (ch 12 §7)."""

from __future__ import annotations

from typing import Any, Mapping

from services.cowork_agent.storm.comms.calls import CommsError
from services.cowork_agent.storm.comms.tools import TEXT_MAX


def text(args: Mapping[str, Any], key: str, *, required: bool = True, limit: int = TEXT_MAX) -> str | None:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise CommsError("missing_argument", f"{key} is required.")
        return None
    if not isinstance(value, str):
        raise CommsError("invalid_argument", f"{key} must be text.")
    if len(value) > limit:
        raise CommsError("too_long", f"{key} may hold at most {limit} characters.")
    return value


def ids(args: Mapping[str, Any], key: str, *, limit: int) -> list[str]:
    value = args.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise CommsError("invalid_argument", f"{key} must be a list of ids.")
    if len(value) > limit:
        raise CommsError("too_many", f"{key} may name at most {limit}.")
    return list(dict.fromkeys(value))


def flag(args: Mapping[str, Any], key: str, *, default: bool | None = None) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise CommsError("invalid_argument" if key in args else "missing_argument", f"{key} must be true or false.")
    return value


def choice(args: Mapping[str, Any], key: str, allowed: tuple[str, ...], *, required: bool = False) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if value not in allowed:
        raise CommsError("invalid_argument", f"{key} is one of {', '.join(allowed)}.")
    return value


def limit(args: Mapping[str, Any], default: int, *, most: int = 100) -> int:
    value = args.get("limit", default)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= most:
        raise CommsError("invalid_argument", f"limit is a whole number from 1 to {most}.")
    return value
