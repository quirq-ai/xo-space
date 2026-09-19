"""``python -m quirq telemetry <command>``

  usage [days]            the workspace usage summary, every project combined
                          (the same as GET /api/xo-projects/usage/summary; days defaults to 30)
  sources                 every telemetry source with its data path and switch
                          (the same as GET /api/telemetry/sources)
"""

from __future__ import annotations

from services.errors import ServiceError

from . import service


def usage(args: list[str]) -> dict:
    days = 30
    if args:
        if not args[0].isdigit() or not 1 <= int(args[0]) <= 365:
            raise ServiceError("invalid_days", "usage: quirq telemetry usage [days], days between 1 and 365")
        days = int(args[0])
    return service.workspace_usage_summary(days=days).model_dump()


def sources(args: list[str]) -> dict:
    items = service.list_sources()
    return {"items": items, "total": len(items)}


COMMANDS = {"usage": usage, "sources": sources}
