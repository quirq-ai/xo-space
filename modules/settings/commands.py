"""``python -m quirq settings <command>``

  settings                the effective runtime settings beside the saved ones, and the restart state
  modules                 the switch table: every module with its effective switches (what GET /api/modules answers)
"""

from __future__ import annotations

from services import modules as registry

from . import service


def settings_(args: list[str]) -> dict:
    return service.overview()


def modules_(args: list[str]) -> dict:
    return registry.describe()


COMMANDS = {"settings": settings_, "modules": modules_}
