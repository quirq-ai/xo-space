"""Report Scout through the existing chat and transcript HTTP contracts.

Every artifact, project, session, and command log stays in temporary roots.
The golden fixture is a real browser export; these tests run its bundled
runtime, never an LLM or network service.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI


FIXTURES = Path(__file__).parent / "fixtures" / "fly"


def events(text: str) -> list[tuple[str, dict]]:
    """Decode named SSE messages, ignoring comments and heartbeat framing."""
    parsed = []
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        kind = "message"
        data = []
        for line in frame.splitlines():
            if line.startswith("event: "):
                kind = line[7:]
            elif line.startswith("data: "):
                data.append(line[6:])
        if data:
            parsed.append((kind, json.loads("\n".join(data))))
    return parsed


class FlyChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fly-chat-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.projects = self.root / "projects"
        self.state.mkdir()
        self.projects.mkdir()
        env = patch.dict(os.environ, {
            "AGENT_NAME": "fly",
            "QUIRQ_STATE_ROOT": str(self.state),
            "XO_PROJECTS_ROOT": str(self.projects),
            "CLAUDE_COWORK_ROOT": str(self.root / "legacy-projects"),
            "QUIRQ_SKIP_BOOT_INSTALL": "1",
        })
        env.start()
        self.addCleanup(env.stop)

        # Tests elsewhere may have resolved a different active manifest first.
        from services.cowork_agent.registry import agent_registry
        for name in ("_MANIFESTS", "_DEFAULT"):
            patcher = patch.object(agent_registry, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)

        from routers.cowork_agent import chat, sessions
        from services.cowork_agent.adapters.fly import adapter, routes
        from services.cowork_agent.engine.chat_state import active_streams
        from services.cowork_agent.registry import adapter_registry
        self.chat = chat
        self.adapter = adapter
        self.active_streams = active_streams
        active_streams.clear()
        chat._recently_started.clear()
        self.addCleanup(active_streams.clear)
        self.addCleanup(chat._recently_started.clear)
        # Session discovery must not look in the developer's real agent homes.
        for owner in (adapter_registry, sessions):
            patcher = patch.object(owner, "list_adapters", return_value=["fly"])
            patcher.start()
            self.addCleanup(patcher.stop)

        self.artifact = json.loads((FIXTURES / "failure-scout.fly.json").read_text())
        self.model = "fly/" + self.artifact["fly"]["id"]
        flies = self.state / "flies"
        flies.mkdir()
        self.artifact_path = flies / "failure-scout.fly.json"
        self.artifact_path.write_text(json.dumps(self.artifact))
        self.workspace = json.loads((FIXTURES / "workspace.json").read_text())
        self.project = self.make_project("failure-demo")
        self.app = FastAPI()
        self.app.include_router(chat.router)
        self.app.include_router(sessions.router)
        self.app.include_router(routes.router)

    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    def make_project(self, name: str) -> Path:
        path = self.projects / name
        (path / ".xo").mkdir(parents=True)
        (path / ".xo" / "project.json").write_text(json.dumps({
            "name": name, "pid": str(uuid.uuid4()), "display_name": name,
        }))
        for file in self.workspace["files"]:
            target = path / file["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(file["content"])
        return path

    async def prompt(self, **extra) -> httpx.Response:
        return await self.client.post("/api/chat/prompt", json={
            "text": "/run",
            "agent_name": "fly", "agent_id": "failure-demo", "model": self.model,
            **extra,
        })

    async def complete(self, **extra) -> tuple[dict, list[tuple[str, dict]]]:
        response = await self.prompt(**extra)
        self.assertEqual(response.status_code, 200, response.text)
        ids = response.json()
        stream = await self.client.get("/api/chat/stream/" + ids["stream_id"])
        self.assertEqual(stream.status_code, 200, stream.text)
        self.assertTrue(stream.headers["content-type"].startswith("text/event-stream"))
        parsed = events(stream.text)
        self.assertFalse([value for kind, value in parsed if kind in {"error", "agent-error"}], parsed)
        done = [value for kind, value in parsed if kind == "done"]
        self.assertEqual(len(done), 1, parsed)
        self.assertEqual(done[0]["session_id"], ids["session_id"])
        return ids, parsed

    def session_document(self, sid: str) -> dict:
        return json.loads((self.state / "fly-runtime" / "sessions" / f"{sid}.json").read_text())

    async def test_prompt_stream_and_persisted_transcript_share_public_session_id(self) -> None:
        ids, parsed = await self.complete()
        sid = ids["session_id"]
        self.assertEqual([v["session_id"] for k, v in parsed if k == "session-created"], [sid])
        text = "".join(v.get("text", "") for k, v in parsed if k == "text-delta")
        self.assertIn(r"AUTH\_EXPIRED", text)
        self.assertIn(r"TOKEN\_REFRESH\_FAILED", text)
        self.assertIn("results/test-01/telemetry-2.json", text)
        self.assertIn("partial", text.lower())

        messages = await self.client.get(f"/api/messages/{sid}")
        self.assertEqual(messages.status_code, 200)
        rows = messages.json()["messages"]
        self.assertEqual([m["data"]["role"] for m in rows], ["user", "assistant"])
        self.assertTrue(all(m["session_id"] == sid for m in rows))
        self.assertTrue(all(p["session_id"] == sid for m in rows for p in m["parts"]))
        transcript = await self.client.get(f"/api/sessions/{sid}/transcript")
        self.assertEqual(transcript.status_code, 200, transcript.text)
        self.assertIn(r"AUTH\_EXPIRED", transcript.json()["messages"][1]["content"])
        sessions = (await self.client.get("/api/sessions")).json()
        row = next(item for item in sessions if item["id"] == sid)
        self.assertEqual(row["agent"], "failure-demo")
        self.assertEqual(Path(row["directory"]), self.project.resolve())
        self.assertNotIn(ids["stream_id"], self.active_streams)
        run_id = self.session_document(sid)["runs"][0]["id"]
        record = await self.client.get(f"/api/fly/runs/{run_id}")
        self.assertEqual(record.status_code, 200, record.text)
        result = record.json()["result"]
        self.assertEqual(record.json()["session_id"], sid)
        self.assertEqual(len(result["trace"]), 4)
        self.assertEqual(len(result["report"]["records"]), 3)
        self.assertEqual(result["report"]["status"], "partial")
        self.assertEqual({r["values"]["error_code"] for r in result["report"]["records"]},
                         {"AUTH_EXPIRED", "TOKEN_REFRESH_FAILED"})
        self.assertEqual(record.json()["artifact_digest"], self.artifact["integrity"]["digest"])

    async def test_resume_uses_pinned_fly_project_and_appends_one_turn(self) -> None:
        ids, _ = await self.complete()
        sid = ids["session_id"]
        response = await self.client.post("/api/chat/prompt", json={
            "text": "/run", "session_id": sid,
        })
        self.assertEqual(response.status_code, 200, response.text)
        resumed = response.json()
        self.assertEqual(resumed["session_id"], sid)
        stream = await self.client.get("/api/chat/stream/" + resumed["stream_id"])
        parsed = events(stream.text)
        self.assertEqual([v["session_id"] for k, v in parsed if k == "done"], [sid])
        self.assertFalse([v for k, v in parsed if k in {"error", "agent-error"}], parsed)
        messages = (await self.client.get(f"/api/messages/{sid}")).json()["messages"]
        self.assertEqual([m["data"]["role"] for m in messages], ["user", "assistant", "user", "assistant"])

    async def test_existing_session_rejects_model_project_and_directory_rebinding(self) -> None:
        ids, _ = await self.complete()
        sid = ids["session_id"]
        self.make_project("other-project")
        for change in ({"model": "fly/another-fly"}, {"agent_id": "other-project"}):
            with self.subTest(change=change):
                response = await self.prompt(session_id=sid, **change)
                self.assertIn(response.status_code, (400, 404, 409), response.text)
        rebind = await self.client.patch(f"/api/sessions/{sid}", json={"directory": str(self.projects / "other-project")})
        self.assertIn(rebind.status_code, (400, 409), rebind.text)
        messages = (await self.client.get(f"/api/messages/{sid}")).json()["messages"]
        self.assertEqual(len(messages), 2)

    async def test_distinct_flies_remain_isolated_and_cannot_replace_a_session_model(self) -> None:
        second = json.loads((FIXTURES / "second-scout.fly.json").read_text())
        (self.state / "flies" / "second.fly.json").write_text(json.dumps(second))
        first_ids, _ = await self.complete()
        second_model = "fly/" + second["fly"]["id"]
        second_ids, _ = await self.complete(model=second_model)
        self.assertNotEqual(first_ids["session_id"], second_ids["session_id"])
        for ids, model in ((first_ids, self.model), (second_ids, second_model)):
            document = self.session_document(ids["session_id"])
            self.assertEqual(document["model"], model)
            rows = (await self.client.get("/api/messages/" + ids["session_id"])).json()["messages"]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["session_id"] == ids["session_id"] for row in rows))
        mismatch = await self.prompt(session_id=first_ids["session_id"], model=second_model)
        self.assertEqual(mismatch.status_code, 409, mismatch.text)

    async def test_reconnect_does_not_run_or_persist_the_turn_again(self) -> None:
        ids, _ = await self.complete()
        again = await self.client.get("/api/chat/stream/" + ids["stream_id"])
        self.assertEqual(len([v for k, v in events(again.text) if k == "done"]), 1)
        rows = (await self.client.get("/api/messages/" + ids["session_id"])).json()["messages"]
        self.assertEqual(len(rows), 2)

    async def test_resume_retains_checkpoint_after_deployment_file_is_removed(self) -> None:
        ids, _ = await self.complete()
        self.artifact_path.unlink()
        await self.complete(session_id=ids["session_id"])
        document = self.session_document(ids["session_id"])
        self.assertIn(self.artifact["integrity"]["digest"], json.dumps(document))

    async def test_aborting_before_sse_does_not_leave_session_busy(self) -> None:
        response = await self.prompt()
        self.assertEqual(response.status_code, 200, response.text)
        ids = response.json()
        abort = await self.client.post("/api/chat/abort", json={"stream_id": ids["stream_id"]})
        self.assertEqual(abort.status_code, 200)
        await self.complete(session_id=ids["session_id"])

    async def test_rejects_unscoped_unknown_and_traversal_project_requests(self) -> None:
        for project in (None, "missing-project", "../failure-demo", str(self.project)):
            with self.subTest(project=project):
                response = await self.prompt(agent_id=project)
                self.assertIn(response.status_code, (400, 404, 422), response.text)
        response = await self.prompt(session_id=str(uuid.uuid4()))
        self.assertIn(response.status_code, (400, 404, 409), response.text)

    async def test_invalid_export_is_rejected_before_a_stream_is_created(self) -> None:
        self.artifact["policy"]["weights"][0] += 1
        self.artifact_path.write_text(json.dumps(self.artifact))
        response = await self.prompt()
        self.assertIn(response.status_code, (400, 404, 409, 422), response.text)
        self.assertFalse(self.active_streams)

    async def test_help_does_not_execute_a_workspace_run(self) -> None:
        with patch.object(self.adapter.runtime, "execute") as execute:
            ids, parsed = await self.complete(text="/help")
        execute.assert_not_called()
        text = "".join(v.get("text", "") for k, v in parsed if k == "text-delta")
        self.assertIn("/run", text)
        self.assertIn("does not interpret general instructions", text)
        self.assertEqual(self.session_document(ids["session_id"])["runs"][0]["status"], "completed")

    async def test_commands_cannot_supply_paths_tools_or_unbounded_reads(self) -> None:
        for command in (
            "Inspect and repair the failing jobs",
            '/run {"workspace":"/etc"}',
            '/run {"command":"cat /etc/passwd"}',
            '/run {"tools":["shell"]}',
            '/run {"maxReads":1000000}',
            '/run {"maxReads":true}',
            '/run {"fields":[]}',
            '/run []',
        ):
            with self.subTest(command=command):
                response = await self.prompt(text=command)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.active_streams)

    async def test_standard_abort_cancels_running_execution_and_persists_status(self) -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def blocked_execute(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.set()
                raise

        # Replace only execution latency, leaving prompt validation, adapter
        # orchestration, SSE transport, cancellation, and persistence real.
        with patch.object(self.adapter.runtime, "execute", side_effect=blocked_execute):
            response = await self.prompt()
            self.assertEqual(response.status_code, 200, response.text)
            ids = response.json()
            stream_task = asyncio.create_task(self.client.get("/api/chat/stream/" + ids["stream_id"]))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                abort = await self.client.post("/api/chat/abort", json={"stream_id": ids["stream_id"]})
                self.assertEqual(abort.status_code, 200)
                await asyncio.wait_for(stopped.wait(), timeout=3)
                stream = await asyncio.wait_for(stream_task, timeout=3)
            finally:
                if not stream_task.done():
                    stream_task.cancel()
                    await asyncio.gather(stream_task, return_exceptions=True)
        parsed = events(stream.text)
        self.assertEqual([v["session_id"] for k, v in parsed if k == "done"], [ids["session_id"]])
        self.assertIn("cancelled", json.dumps(self.session_document(ids["session_id"])).lower())
        # An aborted run releases the session so a subsequent prompt can run.
        await self.complete(session_id=ids["session_id"])

    async def test_duplicate_sse_subscribers_do_not_duplicate_work_or_allow_concurrent_turns(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        original_execute = self.adapter.runtime.execute
        executions = 0

        async def gated_execute(*args, **kwargs):
            nonlocal executions
            executions += 1
            started.set()
            await release.wait()
            return await original_execute(*args, **kwargs)

        with patch.object(self.adapter.runtime, "execute", side_effect=gated_execute):
            response = await self.prompt()
            self.assertEqual(response.status_code, 200, response.text)
            ids = response.json()
            url = "/api/chat/stream/" + ids["stream_id"]
            first = asyncio.create_task(self.client.get(url))
            second = None
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                overlapping = await self.prompt(session_id=ids["session_id"])
                self.assertEqual(overlapping.status_code, 409, overlapping.text)
                second = asyncio.create_task(self.client.get(url))
                await asyncio.sleep(0)
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(first, second), timeout=10)
            finally:
                release.set()
                pending = [task for task in (first, second) if task is not None and not task.done()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        self.assertEqual(executions, 1)
        for stream in responses:
            self.assertEqual([v["session_id"] for k, v in events(stream.text) if k == "done"], [ids["session_id"]])
        rows = (await self.client.get("/api/messages/" + ids["session_id"])).json()["messages"]
        self.assertEqual(len(rows), 2)

    async def pending_run(self) -> tuple[dict, str]:
        """The durable state just before execution finishes (no background task)."""
        response = await self.prompt()
        self.assertEqual(response.status_code, 200, response.text)
        ids = response.json()
        self.active_streams.pop(ids["stream_id"])
        store = self.adapter.sessions
        session = store.load(ids["session_id"])
        run_id = str(uuid.uuid4())
        session["runs"].append({"id": run_id, "status": "running", "started_at": store.now()})
        store.append_message(session, "user", "/run", run_id=run_id)
        store.save(session)
        return session, run_id

    async def test_recovery_keeps_report_committed_before_session_save_crashed(self) -> None:
        session, run_id = await self.pending_run()
        store = self.adapter.sessions
        result = {"report": {"status": "partial", "records": []}, "trace": []}
        with patch.object(store, "save", side_effect=OSError("simulated crash after run write")):
            with self.assertRaises(OSError):
                store.finish(session, run_id, "completed", "Verified completed report", result)
        path = self.state / "fly-runtime" / "runs" / f"{run_id}.json"
        committed = path.read_bytes()
        self.assertEqual(store.load(session["id"])["runs"][0]["status"], "running")
        with patch.object(self.adapter.runtime, "execute") as execute:
            response = await self.client.get("/api/messages/" + session["id"])
            repeated = await self.client.get("/api/messages/" + session["id"])
        execute.assert_not_called()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), repeated.json())
        messages = response.json()["messages"]
        self.assertEqual([m["data"]["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["parts"][0]["data"]["text"], "Verified completed report")
        self.assertEqual(store.load(session["id"])["runs"][0]["status"], "completed")
        self.assertEqual(path.read_bytes(), committed)
        self.assertEqual(store.get_run(run_id)["result"], result)

    async def test_retrying_finish_after_session_save_failure_preserves_first_completion(self) -> None:
        session, run_id = await self.pending_run()
        store = self.adapter.sessions
        with patch.object(store, "save", side_effect=OSError("session save failed")):
            with self.assertRaises(OSError):
                store.finish(session, run_id, "completed", "The original report", {"trace": []})
        original = store.get_run(run_id)
        # Adapter error handling may try to mark this failed after save raises.
        store.finish(session, run_id, "failed", "A later storage error")
        self.assertEqual(store.get_run(run_id), original)
        persisted = store.load(session["id"])
        self.assertEqual(persisted["runs"][0]["status"], "completed")
        self.assertEqual([m["text"] for m in persisted["messages"] if m["role"] == "assistant"],
                         ["The original report"])

    async def test_live_save_failure_after_report_commit_recovers_without_false_error(self) -> None:
        store = self.adapter.sessions
        original_save = store.save
        failed_once = False

        def fail_first_completed_save(session):
            nonlocal failed_once
            if not failed_once and any(run["status"] == "completed" for run in session["runs"]):
                failed_once = True
                raise OSError("simulated transient session publication failure")
            original_save(session)

        with patch.object(store, "save", side_effect=fail_first_completed_save):
            ids, parsed = await self.complete()
        self.assertTrue(failed_once)
        text = "".join(v.get("text", "") for k, v in parsed if k == "text-delta")
        self.assertIn(r"AUTH\_EXPIRED", text)
        session = store.load(ids["session_id"])
        self.assertEqual(session["runs"][0]["status"], "completed")
        self.assertEqual(len(session["messages"]), 2)
        self.assertEqual(session["messages"][1]["text"], text)
        self.assertEqual(store.get_run(session["runs"][0]["id"])["status"], "completed")

    async def test_recovery_marks_a_run_without_a_terminal_record_interrupted_once(self) -> None:
        session, run_id = await self.pending_run()
        store = self.adapter.sessions
        self.assertIsNone(store.get_run(run_id))
        for _ in range(2):
            response = await self.client.get("/api/messages/" + session["id"])
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(store.get_run(run_id)["status"], "interrupted")
        self.assertEqual(len(store.load(session["id"])["messages"]), 2)

    async def test_recovery_preserves_and_refuses_a_mismatched_terminal_record(self) -> None:
        from services.errors import ServiceError
        session, run_id = await self.pending_run()
        store = self.adapter.sessions
        with patch.object(store, "save", side_effect=OSError("session save failed")):
            with self.assertRaises(OSError):
                store.finish(session, run_id, "completed", "The report", {"trace": []})
        path = self.state / "fly-runtime" / "runs" / f"{run_id}.json"
        record = json.loads(path.read_text())
        record["pid"] = str(uuid.uuid4())
        path.write_text(json.dumps(record))
        before = path.read_bytes()
        with self.assertRaises(ServiceError) as caught:
            store.recover(store.load(session["id"]))
        self.assertEqual(caught.exception.code, "invalid_state")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(store.load(session["id"])["runs"][0]["status"], "running")

    async def test_changed_project_identity_between_prompt_and_sse_prevents_execution(self) -> None:
        response = await self.prompt()
        self.assertEqual(response.status_code, 200, response.text)
        ids = response.json()
        metadata_path = self.project / ".xo" / "project.json"
        metadata = json.loads(metadata_path.read_text())
        metadata["pid"] = str(uuid.uuid4())
        metadata_path.write_text(json.dumps(metadata))
        with patch.object(self.adapter.runtime, "execute") as execute:
            stream = await self.client.get("/api/chat/stream/" + ids["stream_id"])
        execute.assert_not_called()
        parsed = events(stream.text)
        self.assertTrue([v for k, v in parsed if k == "agent-error"], parsed)
        self.assertEqual([v["session_id"] for k, v in parsed if k == "done"], [ids["session_id"]])

    async def test_replaced_unscaffolded_project_cannot_reuse_pending_session(self) -> None:
        (self.project / ".xo" / "project.json").unlink()
        response = await self.prompt()
        self.assertEqual(response.status_code, 200, response.text)
        ids = response.json()
        self.project.rename(self.projects / "failure-demo-original")
        self.project.mkdir()
        with patch.object(self.adapter.runtime, "execute") as execute:
            stream = await self.client.get("/api/chat/stream/" + ids["stream_id"])
        execute.assert_not_called()
        parsed = events(stream.text)
        self.assertTrue([v for k, v in parsed if k == "agent-error"], parsed)
        self.assertEqual([v["session_id"] for k, v in parsed if k == "done"], [ids["session_id"]])


if __name__ == "__main__":
    unittest.main()
