"""Routes that run as qq commands (routers/qq_ops.py): the server side (qq_first and the routes
for sharing, project add/remove, backup and restore) and the process side (python -m routers.qq_ops)."""
from __future__ import annotations

import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from routers import qq_ops
from routers.cowork_agent import xo_projects_sync
from routers.cowork_agent.bff import project_management, project_sharing
from services import qq_runner


def answer(code: int, data: dict) -> AsyncMock:
    return AsyncMock(return_value=qq_runner.QQResult(code, data, ""))


class QQFirstTests(unittest.TestCase):
    def call(self, run_qq: AsyncMock, **kw):
        in_process = AsyncMock(return_value={"from": "in-process"})
        with patch.object(qq_runner, "run_qq", run_qq):
            result = asyncio.run(qq_ops.qq_first(["backup", "--project=a"], 5, in_process, **kw))
        return result, in_process

    def test_the_answer_comes_from_qq(self):
        result, in_process = self.call(answer(0, {"ok": True}))
        self.assertEqual(result, {"ok": True})
        in_process.assert_not_awaited()

    def test_a_list_answer_is_unwrapped(self):
        result, _ = self.call(answer(0, {"results": [{"project_id": "a"}]}), results=True)
        self.assertEqual(result, [{"project_id": "a"}])

    def test_an_error_object_is_the_routes_own_http_error(self):
        detail = {"error": "project_exists", "detail": "exists", "suggestion": "force"}
        with self.assertRaises(HTTPException) as cm:
            self.call(answer(1, {"code": "project_exists", "message": "m", "status": 409, "detail": detail}))
        self.assertEqual((cm.exception.status_code, cm.exception.detail), (409, detail))

    def test_not_run_falls_back_in_process(self):
        result, in_process = self.call(AsyncMock(side_effect=qq_runner.QQNotRun("qq is not on PATH")))
        self.assertEqual(result, {"from": "in-process"})
        in_process.assert_awaited_once()

    def test_no_answer_is_a_502_and_never_runs_it_twice(self):
        with self.assertRaises(HTTPException) as cm:
            self.call(AsyncMock(side_effect=qq_runner.QQNoAnswer("timed out")))
        self.assertEqual((cm.exception.status_code, cm.exception.detail["code"]), (502, "qq_no_answer"))


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        for module in (project_sharing, project_management, xo_projects_sync):
            app.include_router(module.router)
        self.client = TestClient(app)

    def qq(self, data: dict, code: int = 0) -> AsyncMock:
        mock = answer(code, data)
        runner = patch.object(qq_runner, "run_qq", mock)
        runner.start()
        self.addCleanup(runner.stop)
        return mock

    def test_share_runs_qq_then_nudges_the_relay_here(self):
        mock = self.qq({"ok": True, "repo": "github.com/a/app"})
        with patch.object(project_sharing.service, "check_now") as nudge:
            res = self.client.post("/api/xo-projects/app/share", json={"workspace_id": "ws-2"})
        self.assertEqual((res.status_code, res.json()["repo"]), (200, "github.com/a/app"))
        self.assertEqual(mock.await_args.args[0], ["share", "--project=app", "--space=ws-2"])
        nudge.assert_called_once_with()

    def test_share_validates_before_qq(self):
        mock = self.qq({"ok": True})
        res = self.client.post("/api/xo-projects/app/share", json={"workspace_id": ""})
        self.assertEqual(res.status_code, 422)
        mock.assert_not_awaited()

    def test_project_add_keeps_201_and_its_guard(self):
        mock = self.qq({"project_id": "app", "created": True})
        res = self.client.post("/api/xo-projects", json={"project_id": "app", "repository_url": "https://github.com/a/app"})
        self.assertEqual(res.status_code, 201)
        self.assertEqual(mock.await_args.args[0],
                         ["projects", "add", "--project=app", "--url=https://github.com/a/app"])
        mock.reset_mock()
        res = self.client.post("/api/xo-projects", json={"project_id": "app", "repository_url": "https://github.com/a/app"},
                               headers={"origin": "https://elsewhere.example"})
        self.assertEqual(res.status_code, 403)   # the guard runs in the route, before qq
        mock.assert_not_awaited()

    def test_restore_all_passes_pins_and_force(self):
        mock = self.qq({"results": [{"project_id": "a"}]})
        res = self.client.post("/api/xo-projects-sync/all/restore",
                               json={"snapshot_id_map": {"a": "20261009-120000"}, "force": True})
        self.assertEqual(res.json(), [{"project_id": "a"}])
        self.assertEqual(mock.await_args.args[0], ["restore", "--all", "--pin=a=20261009-120000", "--force"])

    def test_backup_error_keeps_the_old_shape(self):
        detail = {"error": "backup_failed", "detail": "gpg died"}
        self.qq({"code": "backup_failed", "message": "gpg died", "status": 500, "detail": detail}, code=1)
        res = self.client.post("/api/xo-projects-sync/projects/app", json={"note": "n"})
        self.assertEqual((res.status_code, res.json()["detail"]), (500, detail))


class ProcessSideTests(unittest.TestCase):
    def run_op(self, argv: list[str], op):
        out = io.StringIO()
        with patch.object(qq_ops, "_load_settings"), patch.dict(qq_ops.OPS, {argv[0]: op}), redirect_stdout(out):
            code = qq_ops.main(argv)
        return code, out.getvalue()

    def test_json_stdout_is_only_the_object(self):
        async def op(a):
            print("progress line from the service")
            return {"ok": True, "repo": a.project}
        code, out = self.run_op(["share", "--project=app", "--space=ws", "--json"], op)
        self.assertEqual((code, json.loads(out)), (0, {"ok": True, "repo": "app"}))

    def test_a_value_that_looks_like_a_flag_stays_a_value(self):
        seen = {}

        async def op(a):
            seen.update(project=a.project, all=a.all)
            return {"project_id": a.project}
        self.run_op(["backup", "--project=--all", "--json"], op)
        self.assertEqual(seen, {"project": "--all", "all": False})

    def test_http_error_becomes_the_error_object_and_exit_1(self):
        async def op(a):
            raise HTTPException(404, detail={"code": "project_not_found", "message": "Project not found."})
        code, out = self.run_op(["apply", "--project=x", "--json"], op)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out), {"code": "project_not_found", "message": "Project not found.",
                                           "status": 404, "detail": {"code": "project_not_found",
                                                                     "message": "Project not found."}})

    def test_a_list_response_is_wrapped_and_final(self):
        async def op(a):
            return JSONResponse([{"project_id": "a", "error": "gpg died"}])
        code, out = self.run_op(["backup", "--all", "--json"], op)
        self.assertEqual((code, json.loads(out)), (0, {"results": [{"project_id": "a", "error": "gpg died"}]}))

    def test_missing_values_are_usage_errors(self):
        code, _ = self.run_op(["share", "app", "--json"], AsyncMock())
        self.assertEqual(code, 2)

    def test_remove_without_yes_is_a_dry_run(self):
        seen = {}

        async def op(a):
            seen["confirm"] = a.confirm
            return {"project_id": a.project, "can_remove": True, "blockers": []}
        self.run_op(["projects-remove", "app"], op)
        self.assertIsNone(seen["confirm"])
        self.run_op(["projects-remove", "app", "--yes"], op)
        self.assertEqual(seen["confirm"], "app")


if __name__ == "__main__":
    unittest.main()
