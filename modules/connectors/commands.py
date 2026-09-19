"""``python -m quirq connectors <command>``

  status                  every connector's status: the Composio key, GitHub,
                          Vercel, the Google Drive and OneDrive remotes, MagicPath
"""

from __future__ import annotations

from . import service


async def status(args: list[str]) -> dict:
    return await service.status()


COMMANDS = {"status": status}
