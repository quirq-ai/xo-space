"""Storm's conversation layer on the fake claude CLI: the engine, the review
queue with the conversation's hooks, the chats, and storm's routes served by
a real server on a free loopback port, so a fake CLI calls ``xo_comms`` over
HTTP exactly as the real one does (``tests/fakes`` ``mcp`` step).

``PORT`` is set to that port before any session is made, so the comms mount
a preset builds points at this server (``comms.tools.endpoint_url``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import unittest
from typing import Any, Sequence

import httpx
import uvicorn
from fastapi import FastAPI

from routers.browser_guard import add_browser_write_guard
from routers.cowork_agent.storm import storm_routers
from services.cowork_agent.storm import chats as storm_chats
from services.cowork_agent.storm.chats.conversation import Conversation
from services.cowork_agent.storm.comms.tools import PATH
from services.cowork_agent.storm.engine.memory import MemoryReading
from services.cowork_agent.storm.review.queue import ReviewQueue
from services.cowork_agent.storm.store import StoredEvent
from tests.fakes import by_prompt, claude, install
from tests.storm_engine_kit import AGENT, Kit


def app() -> FastAPI:
    application = FastAPI()
    for router in storm_routers:
        application.include_router(router)
    add_browser_write_guard(application, [])
    return application


class Commits:
    """A listener that keeps each commit's events together, to show what one
    transaction wrote."""

    def __init__(self) -> None:
        self.batches: list[list[StoredEvent]] = []

    def committed(self, events: Sequence[StoredEvent]) -> None:
        self.batches.append(list(events))

    def ephemeral(self, event: Any, epoch: int) -> None:
        pass

    def with_type(self, type_: str) -> list[list[StoredEvent]]:
        return [batch for batch in self.batches if any(e.type == type_ for e in batch)]


class CommsTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.commits = Commits()
        self.queue = ReviewQueue(Conversation())
        self.server = uvicorn.Server(uvicorn.Config(app(), host="127.0.0.1", port=0, log_level="warning",
                                                   lifespan="off"))
        self.serving = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        os.environ["PORT"] = str(self.port)            # both restored by the kit's env patch
        os.environ["AGENT_NAME"] = AGENT
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}")
        self.chats: storm_chats.Chats | None = None

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        await storm_chats.shutdown()
        await self.queue.stop()
        self.server.should_exit = True
        await self.serving
        await self.kit.close()

    async def boot(self, steps: list[dict] | None = None) -> storm_chats.Chats:
        """The engine, the queue and the chats. With ``steps``, every session
        process is a fake claude playing them; without, no process starts
        (the memory guard holds every session back), so a test calls the
        tools itself with tokens it mints for the sessions' epochs."""
        self.kit.scenario(steps or [])
        if steps is None:
            self.kit.reading = MemoryReading(pressure_avg10=100.0)
        engine = await self.kit.boot(desk=self.queue, listeners=[self.commits])
        await self.queue.start(engine)
        self.chats = storm_chats.boot(engine, self.queue)
        return self.chats

    async def boot_roles(self, roles: dict[str, list[dict]]) -> storm_chats.Chats:
        """Like ``boot``, with one fake playing each role's script: a session
        plays the script whose key its system prompt holds."""
        self.kit.fake = install("claude", self.kit.root / "bin",
                                by_prompt(roles, argv=[claude.version(), claude.auth_status()]))
        os.environ["CLAUDE_CLI_PATH"] = str(self.kit.fake.path)
        engine = await self.kit.boot(desk=self.queue, listeners=[self.commits])
        await self.queue.start(engine)
        self.chats = storm_chats.boot(engine, self.queue)
        return self.chats

    # ── making sessions and their tokens ─────────────────────────────────

    async def project(self, name: str = "demo") -> dict:
        (self.kit.projects / name).mkdir(exist_ok=True)
        return await self.chats.project(name)

    async def coordinator(self, chat: dict) -> str:
        return await self.chats.with_coordinator(chat, lambda w, session_id: session_id)

    async def owner_message(self, chat: dict, text: str, *, thread_ts: str | None = None) -> dict:
        from services.cowork_agent.storm.chats import rows
        return await self.kit.engine.write(lambda w: rows.add_message(
            w, chat_id=chat["id"], thread_ts=thread_ts, author_kind="owner", author_id="owner", text=text))

    async def token(self, session_id: str) -> str:
        """A comms token for the session's current epoch, as an attach mints
        one (the attach itself is tested on the fake CLI)."""
        token = secrets.token_urlsafe(32)
        await self.kit.engine.write(lambda w: w.execute(
            "INSERT INTO comms_tokens (sha256, session_id, epoch, created_at) SELECT ?, id, epoch, ? FROM sessions "
            "WHERE id = ?", (hashlib.sha256(token.encode()).hexdigest(), w.now, session_id)))
        return token

    # ── calling ──────────────────────────────────────────────────────────

    async def rpc(self, token: str | None, method: str, params: dict | None = None, **headers: str) -> httpx.Response:
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return await self.http.post(PATH, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                                "params": params or {}}, headers=headers)

    async def call(self, token: str, tool: str, **arguments: Any) -> dict:
        """A successful tool call's result."""
        result = (await self.rpc(token, "tools/call", {"name": tool, "arguments": arguments})).json()["result"]
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])

    async def refused(self, token: str, tool: str, **arguments: Any) -> str:
        """A refused tool call's sentence."""
        result = (await self.rpc(token, "tools/call", {"name": tool, "arguments": arguments})).json()["result"]
        self.assertTrue(result["isError"], result)
        return result["content"][0]["text"]

    async def tool_names(self, token: str) -> list[str]:
        return [t["name"] for t in (await self.rpc(token, "tools/list")).json()["result"]["tools"]]

    def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        return self.kit.rows(sql, params)

    def wakes(self, session_id: str) -> list[tuple[str, str, str]]:
        """``(id, priority, text)`` of the session's queued wakes, in order."""
        return [(r[0], r[1], r[2]) for r in self.rows(
            "SELECT id, priority, envelope FROM wakes WHERE session_id = ? ORDER BY rowid", (session_id,))]
