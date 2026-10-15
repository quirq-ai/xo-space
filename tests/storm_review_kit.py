"""The review queue on an engine on the fake claude CLI, with its routes
served in the engine's own event loop (``tests/storm_engine_kit.py``)."""

from __future__ import annotations

import unittest
import uuid
from typing import Any

import httpx
from fastapi import FastAPI

from routers.browser_guard import add_browser_write_guard
from routers.cowork_agent.storm import review, rules
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.review.queue import ReviewQueue
from tests.fakes import claude
from tests.storm_engine_kit import AGENT, SID, Kit

PUSH = {"command": "git push origin main"}


def app() -> FastAPI:
    application = FastAPI()
    for router in (review.router, rules.router):
        application.include_router(router)
    add_browser_write_guard(application, [])
    return application


class ReviewTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.queue = ReviewQueue()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://127.0.0.1")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.queue.stop()
        await self.kit.close()

    async def boot(self):
        engine = await self.kit.boot(desk=self.queue)
        await self.queue.start(engine)
        return engine

    def started(self, *after: dict) -> list[dict]:
        return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]

    async def thread_session(self, *, chat: str = "chat_p1", pid: str = "pid-demo", ts: str = "1760518860.000100"):
        engine = self.kit.engine
        await engine.write(lambda w: w.execute(
            "INSERT INTO chats (id, kind, project_pid, project_name, created_at) VALUES (?, 'project', ?, 'demo', ?)",
            (chat, pid, w.now)))
        return await engine.create_session(c.SessionSpec(cwd=str(self.kit.work), permissions=c.PermissionPolicy(
            mode="ask")), agent=AGENT, role="thread", preset="thread", chat_id=chat, thread_ts=ts)

    async def decide(self, item_id: str, **body: Any) -> httpx.Response:
        body.setdefault("idempotency_key", uuid.uuid4().hex)
        return await self.client.post(f"/api/chat/review/{item_id}/decision", json=body)

    async def item(self, kind: str = "permission", *, count: int = 1, session_id: str | None = None) -> dict:
        """The ``count``-th item of ``kind`` opened, as the route shows it."""
        opened = await self.kit.recorder.seen("review.opened", count=count, kind=kind, session_id=session_id)
        return (await self.client.get(f"/api/chat/review/{opened.data['id']}")).json()

    def answers(self, request_id: str) -> list[dict]:
        """What the fake CLI received for a control request, in order."""
        out = []
        for run in self.kit.fake.invocations():
            out += [line["response"]["response"] for line in run["stdin"] if line.get("type") == "control_response"
                    and line["response"].get("request_id") == request_id]
        return out

    def user_texts(self) -> list[str]:
        return [line["message"]["content"][0]["text"] for run in self.kit.fake.invocations()
                for line in run["stdin"] if line.get("type") == "user"]

    def assertRefused(self, response: httpx.Response, status: int, code: str) -> dict:
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (status, code),
                         response.text)
        return response.json()["detail"]
