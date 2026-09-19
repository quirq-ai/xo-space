"""``python -m quirq sharing <command>``

  status                  the relay's snapshot (the same as GET /api/project-sharing/status)
  check                   run the next tick now (the same as Check now)
"""

from __future__ import annotations

from . import service


def status(args: list[str]) -> dict:
    return service.status_snapshot()


def check(args: list[str]) -> dict:
    return service.check_now()


COMMANDS = {"status": status, "check": check}
