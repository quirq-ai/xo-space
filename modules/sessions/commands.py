"""``python -m quirq sessions <command>``

  list [n]                the newest n sessions (default 50) with their purpose
  get <session_id>        one session as the API shows it
"""

from __future__ import annotations

from services.errors import ServiceError

from . import service


def list_(args: list[str]) -> dict:
    limit = int(args[0]) if args else service.LIMIT_DEFAULT
    return {"sessions": service.list_sessions(limit=limit)}


def get(args: list[str]) -> dict:
    if not args:
        raise ServiceError("missing_session_id", "usage: quirq sessions get <session_id>")
    return service.get(args[0])


COMMANDS = {"list": list_, "get": get}
