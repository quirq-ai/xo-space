"""codex's adapter class: its name, its manifest and its health probe.

codex has no plain chat until its storm runtime lands (C1, §0.100): the
flagged ``AgentDispatcher`` chat path it had is gone (storm PR 19, sedc
09 #5), and ``/api/chat/prompt`` refuses it up front (``chats()`` is False).
"""

from __future__ import annotations

import asyncio
from typing import Any

from services.cowork_agent.adapters.base import BaseAgentAdapter


class CodexAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "codex"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    async def health(self) -> dict[str, Any]:
        cli = self.config.get("cli_path") or "codex"
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            version = stdout.decode().strip()
            return {"ok": True, "version": version}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# Stable discovery alias — the dynamic loader resolves
# services.cowork_agent.adapters.<AGENT_NAME>.adapter.Adapter, so every
# adapter module exposes its class under this name.
Adapter = CodexAdapter
