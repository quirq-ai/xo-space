"""HTTP mapping for routers/schedules.py.

Only the mapping is under test (status codes, error bodies); behaviour is
covered by tests/test_scheduler.py. ``routers/schedules.py`` itself imports
only the scheduler and FastAPI, but importing anything under ``routers``
runs the package ``__init__`` chain, which on Windows can reach ``fcntl``; the
module skips itself if that import fails. Run it on Linux/WSL with
AGENT_NAME=claude_code.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.commands import scheduler

try:
    from routers.schedules import router
except ImportError as exc:  # pragma: no cover - platform gate
    raise unittest.SkipTest(f"routers package needs POSIX: {exc}") from exc

PY = sys.executable


def _payload(name: str = "job", every: int = 60) -> dict:
    return {"name": name, "command": {"argv": [PY, "-c", "pass"], "timeout": 30}, "every_seconds": every}


class SchedulerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": self._tmp.name}, clear=False)
        self._env.start()
        scheduler.reset_state()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        for run in list(scheduler._running.values()):
            if run.thread is not None:
                run.thread.join(10)
        scheduler.reset_state()
        self._env.stop()
        self._tmp.cleanup()

    def test_create_list_get_update_delete(self) -> None:
        created = self.client.post("/api/schedules", json=_payload("pull issues"))
        self.assertEqual(created.status_code, 201, created.text)
        job = created.json()
        self.assertTrue(job["id"].startswith("pull-issues-"))
        self.assertFalse(job["running"])

        listed = self.client.get("/api/schedules")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([j["id"] for j in listed.json()["jobs"]], [job["id"]])

        self.assertEqual(self.client.get(f"/api/schedules/{job['id']}").json()["name"], "pull issues")

        updated = self.client.put(f"/api/schedules/{job['id']}", json=_payload("renamed", 120))
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["every_seconds"], 120)

        deleted = self.client.delete(f"/api/schedules/{job['id']}")
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"ok": True, "deleted": job["id"]})
        self.assertEqual(self.client.get(f"/api/schedules/{job['id']}").status_code, 404)

    def test_validation_errors_are_400_with_the_reason(self) -> None:
        bad = self.client.post("/api/schedules", json={**_payload(), "every_seconds": 0})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("every_seconds", bad.json()["detail"])
        shell = self.client.post("/api/schedules", json={
            "name": "x", "command": {"command": "ls | wc", "timeout": 5}, "every_seconds": 60})
        self.assertEqual(shell.status_code, 400)
        not_object = self.client.post("/api/schedules", json=[1, 2])
        self.assertEqual(not_object.status_code, 400)

    def test_unknown_job_is_404_everywhere(self) -> None:
        for method, path in [("get", "/api/schedules/nope-000000"),
                             ("put", "/api/schedules/nope-000000"),
                             ("delete", "/api/schedules/nope-000000"),
                             ("post", "/api/schedules/nope-000000/run"),
                             ("get", "/api/schedules/nope-000000/runs")]:
            with self.subTest(method=method, path=path):
                kwargs = {"json": _payload()} if method == "put" else {}
                self.assertEqual(getattr(self.client, method)(path, **kwargs).status_code, 404)

    def test_run_now_is_202_then_409_while_running_and_runs_are_listed(self) -> None:
        job = self.client.post("/api/schedules", json={
            "name": "block", "every_seconds": 60,
            "command": {"argv": [PY, "-c", "import time; time.sleep(2)"], "timeout": 30}}).json()
        first = self.client.post(f"/api/schedules/{job['id']}/run")
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["ok"], True)
        self.assertTrue(first.json()["job"]["running"])
        second = self.client.post(f"/api/schedules/{job['id']}/run")
        self.assertEqual(second.status_code, 409)

        scheduler._running[job["id"]].thread.join(10)
        scheduler.tick()
        runs = self.client.get(f"/api/schedules/{job['id']}/runs", params={"limit": 5})
        self.assertEqual(runs.status_code, 200)
        self.assertEqual(runs.json()["runs"][0]["trigger"], "manual")
        self.assertEqual(runs.json()["runs"][0]["status"], "ok")

    def test_corrupt_store_is_500_with_the_path(self) -> None:
        scheduler.jobs_file().parent.mkdir(parents=True)
        scheduler.jobs_file().write_text("{", encoding="utf-8")
        res = self.client.get("/api/schedules")
        self.assertEqual(res.status_code, 500)
        self.assertIn("jobs.json", res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
