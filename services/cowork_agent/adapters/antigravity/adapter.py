"""
Antigravity (agy) adapter class: its name, its manifest and its health probe.

agy has no plain chat until its storm runtime lands (C2, §0.100): the
flagged ``AgentDispatcher`` chat path it had (``agy -p`` with its
skip-permissions flag, its answer read back from the transcript) is gone
(storm PR 19, sedc 09 #5), and ``/api/chat/prompt`` refuses it up front
(``chats()`` is False). Login state is still read from the OAuth token
file (``auth.py``); the transcript and token readers (``transcript.py``,
``tokens.py``) serve the ``sessions`` and ``usage`` capabilities.
"""
from __future__ import annotations

import asyncio
from typing import Any

from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.adapters.antigravity.auth import has_usable_login

_BACKEND = "antigravity"


class AntigravityAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return _BACKEND

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    async def health(self) -> dict[str, Any]:
        cli = self.config.get("cli_path") or "agy"
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return {"ok": True, "version": stdout.decode().strip(),
                    "logged_in": has_usable_login()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# Stable discovery alias — the loader resolves
# services.cowork_agent.adapters.<AGENT_NAME>.adapter.Adapter.
Adapter = AntigravityAdapter
