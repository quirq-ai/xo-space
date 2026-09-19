"""``python -m quirq connections <command>``

  list                    every connection with its state
  poll <toolkit>          poll one connection now (the same as Poll now)
  events <toolkit> [n]    the newest n collected events (default 20)
"""

from __future__ import annotations

from services.errors import ServiceError

from . import service


def list_(args: list[str]) -> dict:
    return {"signed_in": service.signed_in(), "connections": service.list_connections()}


async def poll(args: list[str]) -> dict:
    if not args:
        raise ServiceError("missing_toolkit", "usage: quirq connections poll <toolkit>")
    return await service.poll_now(args[0])


def events(args: list[str]) -> dict:
    if not args:
        raise ServiceError("missing_toolkit", "usage: quirq connections events <toolkit> [limit]")
    limit = int(args[1]) if len(args) > 1 else 20
    return {"toolkit": args[0], "events": service.events(args[0], limit=limit)}


COMMANDS = {"list": list_, "poll": poll, "events": events}
