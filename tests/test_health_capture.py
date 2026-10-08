"""Every capture point hands its failure to the health record, and changes
nothing for its caller."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services import background
from services.cowork_agent.visualizer import peers_store, todos_store, workitems_store
from services.cowork_agent.visualizer.watcher import Watcher
from services.health import hooks, recorder
from services.inbox import store as inbox_store


class CaptureSandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.state), "XO_PROJECTS_ROOT": str(self.root / "p")})
        env.start()
        self.addCleanup(env.stop)
        recorder._reset_for_tests(enable_recording=True)
        self.addCleanup(recorder._reset_for_tests)

    def events(self, **match) -> list[dict]:
        recorder.flush()
        folder = self.state / "setup" / "health" / "events"
        found = [json.loads(p.read_text()) for p in folder.glob("*.json")] if folder.exists() else []
        return [e for e in found if all(e.get(k) == v for k, v in match.items())]


class BackgroundTaskTests(CaptureSandbox):
    def run_task(self, name: str, coro, *, finishes_by_design: bool = False) -> None:
        async def main() -> None:
            task = asyncio.ensure_future(coro())
            background.register(name, task, finishes_by_design=finishes_by_design)
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
            await asyncio.sleep(0)  # let the done callback run

        asyncio.run(main())

    def test_a_crashed_task_is_recorded(self) -> None:
        async def crash():
            raise RuntimeError("poller blew up")

        self.run_task("connections poller", crash)
        [event] = self.events(component="connections poller")
        self.assertEqual((event["kind"], event["error_type"], event["message"]), ("crash", "RuntimeError", "poller blew up"))

    def test_an_unexpected_exit_is_recorded_and_a_planned_one_is_not(self) -> None:
        async def done():
            return None

        self.run_task("github poller", done)
        self.run_task("gateway reconcile", done, finishes_by_design=True)
        self.assertEqual([e["kind"] for e in self.events(component="github poller")], ["exit"])
        self.assertEqual(self.events(component="gateway reconcile"), [])

    def test_a_failure_streak_is_recorded_at_each_threshold_only(self) -> None:
        async def idle():
            await asyncio.sleep(0)

        self.run_task("usage sync", idle)
        for _ in range(6):
            background.tick_failed("usage sync", ValueError("bad answer"))
        [event] = self.events(component="usage sync", kind="failing")
        self.assertEqual(event["count"], 2, "at 3 and at 5, not every tick")
        self.assertEqual(event["details"]["consecutive_failures"], 5)


    def test_a_streak_reported_as_text_is_not_a_second_record(self) -> None:
        # The watcher reports "N step(s) failed; first: …" and records each
        # failing step itself; the streak would only repeat it, less precisely.
        async def idle():
            await asyncio.sleep(0)

        self.run_task("watcher", idle)
        for _ in range(6):
            background.tick_failed("watcher", "1 step(s) failed; first: stats: AttributeError: x")
        self.assertEqual(self.events(kind="failing"), [])


class WatcherStepTests(CaptureSandbox):
    def test_every_failing_step_is_recorded_even_without_a_streak(self) -> None:
        # Live test A4: a damaged stats.json fails only ticks with activity, so
        # the consecutive-failure streak never builds; the step is still kept.
        fake = SimpleNamespace(step_errors=[])
        try:
            {}.get("x").get("y")
        except AttributeError as exc:
            Watcher._step_failed(fake, "stats", exc)
        [event] = self.events(component="watcher")
        self.assertEqual((event["kind"], event["subject"], event["error_type"]), ("failing", "stats", "AttributeError"))
        self.assertEqual(len(fake.step_errors), 1, "the watcher's own bookkeeping is unchanged")


class StoreRefusalTests(CaptureSandbox):
    def xo(self, name: str, document: dict) -> Path:
        path = self.root / "p" / "proj" / ".xo" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
        return path

    def test_each_store_refusal_is_kept_with_its_file(self) -> None:
        cases = [
            ("todos", self.xo("todos.json", {"schema": 2, "sessions": []}),
             lambda p: todos_store.create_todo(p, runtime="r", content="x")),
            ("workitems", self.xo("workitems.json", {"schema": 1, "items": []}), workitems_store.list_workitems),
            ("peers", self.xo("peers.json", {"schema": 1, "peers": {}}), peers_store.list_peers),
        ]
        for component, path, use in cases:
            with self.subTest(store=component):
                with self.assertRaises(Exception):
                    use(path)
                [event] = self.events(component=component)
                self.assertEqual((event["kind"], event["error_type"]), ("refusal", "corrupt_document"))
                self.assertEqual(event["subject"], f"<projects>/proj/.xo/{path.name}")

    def test_an_inbox_it_would_wipe_is_kept(self) -> None:
        inbox = self.state / "inbox" / "inbox.json"
        inbox.parent.mkdir(parents=True)
        inbox.write_text(json.dumps({"schema": 1, "items": "oops"}))
        _doc, ok = inbox_store.load_document(inbox)
        self.assertFalse(ok)
        [event] = self.events(component="inbox")
        self.assertEqual((event["kind"], event["subject"]), ("refusal", "<state>/inbox/inbox.json"))
        self.assertIn("items is a str", event["message"])


class HttpTests(CaptureSandbox):
    def app(self, *, record: bool) -> TestClient:
        app = FastAPI()

        @app.get("/boom/{item}")
        def boom(item: str):
            raise RuntimeError(f"broke on {item}")

        @app.get("/missing")
        def missing():
            raise HTTPException(status_code=404, detail="no")

        if record:
            app.add_middleware(hooks.RecordUnhandledErrors)
        return TestClient(app, raise_server_exceptions=False)

    def test_an_unhandled_error_is_kept_by_route_and_the_response_is_unchanged(self) -> None:
        plain, recorded = self.app(record=False).get("/boom/secret-id"), self.app(record=True).get("/boom/secret-id")
        self.assertEqual((recorded.status_code, recorded.text), (plain.status_code, plain.text))
        self.assertEqual(recorded.status_code, 500)
        [event] = self.events(kind="http_500")
        self.assertEqual(event["subject"], "GET /boom/{item}")
        self.assertNotIn("secret-id", event["subject"])

    def test_a_handled_error_is_not_a_failure(self) -> None:
        self.assertEqual(self.app(record=True).get("/missing").status_code, 404)
        self.assertEqual(self.events(), [])


class LoopAndThreadTests(CaptureSandbox):
    def test_a_loop_error_is_kept_and_passed_on(self) -> None:
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        previous = MagicMock()
        loop.set_exception_handler(previous)
        hooks.install_loop_hook(loop)
        try:
            raise KeyError("lost")
        except KeyError as exc:
            loop.call_exception_handler({"message": "Task exception was never retrieved", "exception": exc})
        [event] = self.events(component="asyncio")
        self.assertEqual((event["kind"], event["error_type"]), ("crash", "KeyError"))
        previous.assert_called_once()

    def test_a_thread_that_dies_is_kept_and_passed_on(self) -> None:
        original = threading.excepthook
        self.addCleanup(setattr, threading, "excepthook", original)
        passed_on = []
        threading.excepthook = passed_on.append
        hooks.install_thread_hook()

        def work():
            raise ValueError("thread died")

        thread = threading.Thread(target=work, name="sync-worker")
        thread.start()
        thread.join()
        [event] = self.events(component="thread")
        self.assertEqual((event["subject"], event["error_type"]), ("sync-worker", "ValueError"))
        self.assertEqual(len(passed_on), 1)


if __name__ == "__main__":
    unittest.main()
