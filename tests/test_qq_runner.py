"""The server's bridge to qq commands (services/qq_runner.py) and the update pilot's routes."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import space
from services import qq_runner

# A stand-in for qq: QQ_FAKE picks the behaviour, so each test sets up exactly one case.
FAKE_QQ = """#!/bin/sh
case "$QQ_FAKE" in
    ok)      printf '{"args": "%s", "cwd": "%s"}\\n' "$*" "$(pwd)" ;;
    refused) echo '{"updated": false, "reason": "dirty_tree"}'; exit 1 ;;
    garbage) echo 'not json'; echo 'something broke' >&2; exit 2 ;;
    slow)    sleep 5; echo '{}' ;;
    norun)   echo 'qq: no Python environment' >&2; exit 127 ;;
    crash)   echo 'Traceback ...' >&2; exit 1 ;;
esac
"""


class RunQQTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fake = Path(self.tmp.name) / "qq"
        fake.write_text(FAKE_QQ)
        fake.chmod(0o755)
        # The state root too: every run is written to <state>/inbox/activity/commands.log, which
        # must not be the real one.
        path = patch.dict(os.environ, {"PATH": f"{self.tmp.name}{os.pathsep}{os.environ.get('PATH', '')}",
                                       "QUIRQ_STATE_ROOT": str(Path(self.tmp.name) / "state")})
        path.start()
        self.addCleanup(path.stop)
        launcher = patch.object(qq_runner, "QQ", "qq")   # tests/__init__.py turns qq off
        launcher.start()
        self.addCleanup(launcher.stop)

    def run_qq(self, mode: str, *args: str, timeout: float = 10):
        with patch.dict(os.environ, {"QQ_FAKE": mode}):
            return asyncio.run(qq_runner.run_qq(list(args), timeout=timeout))

    def test_object_on_success_with_json_added_and_run_from_the_checkout(self):
        res = self.run_qq("ok", "update-check")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.data["args"], "update-check --json")
        self.assertEqual(os.path.realpath(res.data["cwd"]), os.path.realpath(qq_runner.REPO_ROOT))

    def test_a_refusal_is_a_result_not_an_exception(self):
        res = self.run_qq("refused", "update")
        self.assertEqual((res.returncode, res.data["reason"]), (1, "dirty_tree"))

    def test_output_that_is_not_an_object_is_unavailable_with_the_detail(self):
        with self.assertRaises(qq_runner.QQUnavailable) as cm:
            self.run_qq("garbage", "update")
        self.assertIn("exited 2 without a JSON result", str(cm.exception))
        self.assertIn("something broke", str(cm.exception))

    def test_timeout_is_no_answer(self):
        with self.assertRaises(qq_runner.QQNoAnswer) as cm:
            self.run_qq("slow", "update", timeout=0.5)
        self.assertIn("timed out", str(cm.exception))

    def test_missing_qq_is_unavailable(self):
        with patch.dict(os.environ, {"PATH": self.tmp.name + "/nowhere"}):
            with self.assertRaises(qq_runner.QQNotRun) as cm:
                asyncio.run(qq_runner.run_qq(["update"], timeout=5))
        self.assertIn("not on PATH", str(cm.exception))

    def test_bad_usage_and_no_python_mean_it_never_ran(self):
        for mode in ("garbage", "norun"):   # exit 2 and exit 127
            with self.subTest(mode), self.assertRaises(qq_runner.QQNotRun):
                self.run_qq(mode, "backup")

    def test_a_crash_after_starting_is_no_answer(self):
        with self.assertRaises(qq_runner.QQNoAnswer) as cm:
            self.run_qq("crash", "backup")
        self.assertIn("Traceback", str(cm.exception))


class UpdateRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(space.router)
        self.client = TestClient(app, client=("127.0.0.1", 12345))
        self.remote = TestClient(app, client=("192.0.2.10", 12345))

    def with_qq(self, result=None, error=None):
        mock = AsyncMock(side_effect=error) if error else AsyncMock(return_value=result)
        runner = patch.object(qq_runner, "run_qq", mock)
        runner.start()
        self.addCleanup(runner.stop)
        return mock

    def test_status_comes_from_qq_first(self):
        mock = self.with_qq(qq_runner.QQResult(0, {"supported": True, "behind": 2}, ""))
        with patch("services.cowork_agent.self_update.check_update_status") as in_process:
            res = self.client.get("/space/update/status")
        self.assertEqual((res.status_code, res.json()), (200, {"supported": True, "behind": 2}))
        self.assertEqual(mock.await_args.args[0], ["update-check"])
        in_process.assert_not_called()

    def test_status_error_object_from_qq_is_503(self):
        self.with_qq(qq_runner.QQResult(1, {"code": "update_status_failed", "message": "x"}, ""))
        res = self.client.get("/space/update/status")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (503, "update_status_failed"))

    def test_status_falls_back_in_process_when_qq_cannot_run(self):
        self.with_qq(error=qq_runner.QQUnavailable("qq is not on PATH"))
        with patch("services.cowork_agent.self_update.check_update_status",
                   return_value={"supported": True, "in_process": True}):
            res = self.client.get("/space/update/status")
        self.assertEqual((res.status_code, res.json()), (200, {"supported": True, "in_process": True}))

    def test_apply_refusal_from_qq_stays_a_200_and_is_final(self):
        self.with_qq(qq_runner.QQResult(1, {"updated": False, "reason": "dirty_tree", "message": "m"}, ""))
        with patch("services.cowork_agent.self_update.apply_update") as in_process:
            res = self.client.post("/space/update/apply")
        self.assertEqual((res.status_code, res.json()["reason"]), (200, "dirty_tree"))
        in_process.assert_not_called()

    def test_apply_failure_from_qq_is_409(self):
        self.with_qq(qq_runner.QQResult(1, {"code": "update_failed", "message": "merge failed"}, ""))
        res = self.client.post("/space/update/apply")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (409, "update_failed"))

    def test_apply_falls_back_in_process_when_qq_cannot_run(self):
        self.with_qq(error=qq_runner.QQUnavailable("qq update timed out after 300s"))
        with patch("services.cowork_agent.self_update.apply_update",
                   return_value={"updated": False, "reason": "up_to_date", "message": "m"}):
            res = self.client.post("/space/update/apply")
        self.assertEqual((res.status_code, res.json()["reason"]), (200, "up_to_date"))

    def test_apply_is_still_localhost_only(self):
        mock = self.with_qq(qq_runner.QQResult(0, {"updated": True}, ""))
        self.assertEqual(self.remote.post("/space/update/apply").status_code, 403)
        mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
