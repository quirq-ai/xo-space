"""Storm's run routes (ch 17 §2.3): every route's shape and errors, on the
fake claude CLI, driven in the engine's own event loop."""

from __future__ import annotations

import asyncio
import dataclasses
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI

from routers.cowork_agent.storm import storm_routers
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
from services.cowork_agent.storm.engine import session as session_module
from tests.fakes import claude
from tests.storm_engine_kit import SID, Kit, message
from tests.test_storm_runs import attached, started, write_transcripts

ROW = {"id", "seq", "session_id", "turn_id", "parent_run_id", "native_run_id", "spawn_call_id", "kind", "agent_type",
       "title", "background", "depth", "state", "source", "started_at", "ended_at", "last_activity_at", "tokens",
       "tool_calls", "last_tool", "summary", "transcript_ref", "stop_requested_at", "stopped_by", "epoch", "can_stop"}


def app() -> FastAPI:
    application = FastAPI()
    for router in storm_routers:
        application.include_router(router)
    return application


class NoRunControl(ClaudeRuntime):
    """A runtime that can neither stop one sub-agent nor read its transcript."""

    def features(self) -> c.FeatureList:
        listed = super().features()
        return dataclasses.replace(listed, features={**listed.features,
                                                     "runs.stop": c.FeatureSupport(support="unsupported"),
                                                     "runs.read": c.FeatureSupport(support="unsupported")})


class RunRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://127.0.0.1")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.kit.close()

    def refused(self, response: httpx.Response, status: int, code: str) -> dict:
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (status, code))
        return response.json()["detail"]

    async def test_list_read_and_transcript(self) -> None:
        kit = self.kit
        kit.scenario(started({"sleep": 0.5}, claude.task_started(SID, "task-1", description="Reader"),
                             claude.system(SID, "task_notification", task_id="task-1", status="completed",
                                           summary="read it"),
                             *claude.text_turn(SID, "hi"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        write_transcripts(kit.home, await attached(kit, sid), {"task-1": ({"description": "Reader", "spawnDepth": 1}, [
            {"type": "user", "uuid": "r1", "isSidechain": True, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "z" * 5000}]}}])})
        finished = await kit.recorder.seen("run.finished", session_id=sid)
        run_id = finished.data["run_id"]

        listed = (await self.client.get(f"/api/chat/sessions/{sid}/runs")).json()
        self.assertEqual(set(listed), {"session_id", "runs", "counts", "next_after_seq"})
        (row,) = listed["runs"]
        self.assertEqual(set(row), ROW)
        self.assertEqual((row["id"], row["state"], row["can_stop"], listed["counts"]["total"]),
                         (run_id, "finished", False, 1))
        tree = (await self.client.get(f"/api/chat/sessions/{sid}/runs",
                                      params={"tree": "true", "depth": 2, "state": "finished,failed",
                                              "kind": "subagent"})).json()
        self.assertEqual(set(tree["runs"][0]), ROW | {"children", "child_counts"})
        self.assertEqual((await self.client.get(f"/api/chat/sessions/{sid}/runs",
                                                params={"state": "running"})).json()["runs"], [])
        self.refused(await self.client.get(f"/api/chat/sessions/{sid}/runs", params={"state": "done"}),
                     400, "bad_filter")
        self.refused(await self.client.get(f"/api/chat/sessions/{sid}/runs", params={"kind": "helper"}),
                     400, "bad_filter")
        self.assertEqual((await self.client.get(f"/api/chat/sessions/{sid}/runs", params={"limit": 0})).status_code,
                         422)
        self.refused(await self.client.get("/api/chat/sessions/cse_nope/runs"), 404, "session_not_found")

        one = (await self.client.get(f"/api/chat/runs/{run_id}")).json()
        self.assertEqual(set(one), ROW | {"child_counts", "ancestry"})
        self.assertEqual(set(one["ancestry"]), {"chat", "thread", "session", "runs"})
        self.refused(await self.client.get("/api/chat/runs/run_nope"), 404, "run_not_found")

        page = (await self.client.get(f"/api/chat/runs/{run_id}/transcript", params={"limit": 1})).json()
        self.assertEqual(set(page), {"run_id", "items", "next"})
        block = page["items"][0]["message"]["content"][0]
        self.assertEqual((len(block["content"]), block["bytes"], block["truncated"]), (2048, 5000, True))
        self.refused(await self.client.get(f"/api/chat/runs/{run_id}/transcript", params={"after": "x"}),
                     400, "bad_after")
        self.refused(await self.client.get("/api/chat/runs/run_nope/transcript"), 404, "run_not_found")
        for path in (kit.home / ".claude" / "projects").rglob("agent-task-1.jsonl"):
            path.unlink()
        gone = self.refused(await self.client.get(f"/api/chat/runs/{run_id}/transcript"), 410, "transcript_gone")
        self.assertEqual(gone["summary"], "read it")
        await kit.idle_timer()

    async def test_stop_a_working_run_then_an_ended_one(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"), claude.idle(SID),
                             *claude.host_control("stop_task"), *claude.task_killed(SID, "task-1")))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        run_id = (await kit.recorder.seen("run.started", session_id=sid)).data["run_id"]
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertTrue((await self.client.get(f"/api/chat/runs/{run_id}")).json()["can_stop"])

        stopping = await self.client.post(f"/api/chat/runs/{run_id}/stop")
        self.assertEqual(stopping.status_code, 202)
        self.assertEqual((stopping.json()["stopping"], stopping.json()["run"]["stopped_by"]), (True, "person"))
        self.assertIsNotNone(stopping.json()["run"]["stop_requested_at"])
        await kit.recorder.seen("run.finished", session_id=sid, state="stopped", stopped_by="person")
        again = await self.client.post(f"/api/chat/runs/{run_id}/stop")
        self.assertEqual((again.status_code, again.json()["stopping"], again.json()["run"]["state"]),
                         (200, False, "stopped"))
        self.assertEqual((await self.client.post(f"/api/chat/runs/{run_id}/stop", json={"by": "x"})).status_code, 200)
        self.refused(await self.client.post("/api/chat/runs/run_nope/stop"), 404, "run_not_found")
        await kit.idle_timer()

    async def test_stop_all_stops_every_working_sub_agent_and_leaves_the_root(self) -> None:
        """ch 14 §4.3: each child by stop_task, never the root's interrupt."""
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), claude.task_started(SID, "task-2"),
                             *claude.text_turn(SID, "launched"), claude.idle(SID),
                             *claude.host_control("stop_task"), *claude.host_control("stop_task"),
                             *claude.task_killed(SID, "task-1"), *claude.task_killed(SID, "task-2")))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        ids = sorted(r[0] for r in kit.rows("SELECT id FROM runs WHERE state = 'running'"))
        self.assertEqual(len(ids), 2)
        stopping = await self.client.post(f"/api/chat/sessions/{sid}/runs/stop")
        self.assertEqual((stopping.status_code, sorted(stopping.json()["stopping"])), (202, ids))
        await kit.until(lambda: kit.rows("SELECT count(*) FROM runs WHERE state = 'stopped'")[0][0] == 2)
        sent = [line["request"]["subtype"] for run in kit.runs() for line in run["stdin"] if "request" in line]
        self.assertEqual((sent.count("stop_task"), sent.count("interrupt")), (2, 0))      # the root runs on
        again = await self.client.post(f"/api/chat/sessions/{sid}/runs/stop")
        self.assertEqual((again.status_code, again.json()), (200, {"stopping": []}))
        self.refused(await self.client.post("/api/chat/sessions/cse_nope/runs/stop"), 404, "session_not_found")
        await kit.idle_timer()

    async def test_stop_all_waits_for_a_silent_cli_once_not_per_run(self) -> None:
        """The stops go out together: a CLI that never answers holds the
        session for one grace, not one per run."""
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), claude.task_started(SID, "task-2"),
                             claude.task_started(SID, "task-3"), *claude.text_turn(SID, "launched"), claude.idle(SID),
                             {"sleep": 30}))                              # never answers stop_task
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        loop = asyncio.get_running_loop()
        with mock.patch.object(session_module, "STOP_GRACE_S", 0.5), \
                self.assertLogs("services.cowork_agent.storm.engine.session", "WARNING"):
            began = loop.time()
            stopping = await self.client.post(f"/api/chat/sessions/{sid}/runs/stop")
            took = loop.time() - began
        self.assertEqual((stopping.status_code, len(stopping.json()["stopping"])), (202, 3))
        self.assertLess(took, 1.2)                                       # one grace (0.5 s), not three
        self.assertEqual(kit.rows("SELECT count(*) FROM runs WHERE stop_requested_at IS NOT NULL"), [(3,)])

    async def test_stop_all_on_a_runtime_that_cant_stop_one_is_501_with_or_without_runs(self) -> None:
        kit = self.kit
        kit.scenario(started(*claude.text_turn(SID, "hi"), claude.idle(SID)))
        engine = await kit.boot_with(NoRunControl(claude_home=kit.home / ".claude"))
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(self.refused(await self.client.post(f"/api/chat/sessions/{sid}/runs/stop"), 501,
                                      "unsupported")["feature"], "runs.stop")
        await kit.idle_timer()

    async def test_a_runtime_that_cant_stop_or_read_a_run_is_501(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"), claude.idle(SID)))
        engine = await kit.boot_with(NoRunControl(claude_home=kit.home / ".claude"))
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        run_id = (await kit.recorder.seen("run.started", session_id=sid)).data["run_id"]
        self.assertEqual(self.refused(await self.client.post(f"/api/chat/runs/{run_id}/stop"), 501,
                                      "unsupported")["feature"], "runs.stop")
        self.assertEqual(self.refused(await self.client.get(f"/api/chat/runs/{run_id}/transcript"), 501,
                                      "unsupported")["feature"], "runs.read")
        self.assertEqual(kit.rows("SELECT state, stop_requested_at FROM runs"), [("running", None)])


if __name__ == "__main__":
    unittest.main()
