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
        self.client = TestClient(app, client=("127.0.0.1", 12345))

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

    def test_first_run_at_is_accepted_and_normalised_to_utc(self) -> None:
        res = self.client.post("/api/schedules", json={
            **_payload("weekly", 604800), "first_run_at": "2030-01-07T19:00:00+05:30"})
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json()["first_run_at"], "2030-01-07T13:30:00Z")
        self.assertEqual(res.json()["next_run"], "2030-01-07T13:30:00Z")

        naive = self.client.post("/api/schedules", json={
            **_payload(), "first_run_at": "2030-01-07T19:00:00"})
        self.assertEqual(naive.status_code, 400)
        self.assertIn("offset", naive.json()["detail"])

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

    def test_browser_mutations_require_the_same_loopback_origin(self) -> None:
        client = TestClient(self.client.app, base_url="http://127.0.0.1:5002", client=("127.0.0.1", 12345))
        job = client.post('/api/schedules', json=_payload('origin guarded')).json()
        paths = [('POST', '/api/schedules'), ('PUT', '/api/schedules/'+job['id']),
                 ('DELETE', '/api/schedules/'+job['id']), ('POST', '/api/schedules/'+job['id']+'/run')]
        for origin in ('https://untrusted.example', 'null', 'not an origin',
                       'http://localhost:5002', 'http://127.0.0.1:5003', 'https://127.0.0.1:5002'):
            for method, path in paths:
                with self.subTest(origin=origin, method=method):
                    response = client.request(method, path, headers={'Origin': origin}, json=_payload())
                    self.assertEqual(response.status_code, 403)
        # A simple cross-origin form POST needs no CORS preflight. It must
        # also be denied on the bodyless run route before the executor starts.
        form = client.post('/api/schedules/'+job['id']+'/run',
                           headers={'Origin': 'https://untrusted.example'}, data={'run': '1'})
        self.assertEqual(form.status_code, 403)
        self.assertEqual(scheduler._running, {})
        same = client.put('/api/schedules/'+job['id'], headers={'Origin': 'http://127.0.0.1:5002'},
                          json=_payload('same origin'))
        self.assertEqual(same.status_code, 200, same.text)
        self.assertEqual(client.delete('/api/schedules/'+job['id']).status_code, 200, 'CLI needs no Origin')

    def test_a_declared_public_origin_is_accepted_behind_a_proxy(self) -> None:
        # A Coder workspace: the browser's Origin is the workspace URL and the
        # proxy connects from loopback. The operator declares that URL as
        # QUIRQ_PUBLIC_URL. An undeclared public host stays refused (a DNS-
        # rebinding page would match Host too), and so does a declared host
        # on the wrong scheme or a sibling workspace.
        public = "https://xo-space--shared--x.dev.workspace.helloxo.nl"
        client = TestClient(self.client.app, base_url=public, client=("127.0.0.1", 12345))
        with patch.dict(os.environ, {"QUIRQ_PUBLIC_URL": public + "/", "ALLOWED_ORIGINS": ""}):
            created = client.post("/api/schedules", headers={"Origin": public}, json=_payload("proxied"))
            self.assertEqual(created.status_code, 201, created.text)
            job = created.json()
            edited = client.put(f"/api/schedules/{job['id']}", headers={"Origin": public},
                                json=_payload("edited"))
            self.assertEqual(edited.status_code, 200, edited.text)
            for bad in ("http://xo-space--shared--x.dev.workspace.helloxo.nl",
                        "https://xo-space--shared--y.dev.workspace.helloxo.nl",
                        "https://attacker.example"):
                with self.subTest(origin=bad):
                    self.assertEqual(
                        client.delete(f"/api/schedules/{job['id']}", headers={"Origin": bad}).status_code, 403)
            self.assertEqual(client.delete(f"/api/schedules/{job['id']}", headers={"Origin": public}).status_code, 200)
        # The same request with nothing declared is refused.
        with patch.dict(os.environ, {"QUIRQ_PUBLIC_URL": "", "ALLOWED_ORIGINS": ""}):
            self.assertEqual(
                client.post("/api/schedules", headers={"Origin": public}, json=_payload("undeclared")).status_code, 403)
        # ALLOWED_ORIGINS (the CORS list) declares origins too.
        with patch.dict(os.environ, {"QUIRQ_PUBLIC_URL": "", "ALLOWED_ORIGINS": "http://localhost:3000, " + public}):
            self.assertEqual(
                client.post("/api/schedules", headers={"Origin": public}, json=_payload("via cors list")).status_code, 201)

    def test_same_origin_manual_run_and_default_http_port(self) -> None:
        client = TestClient(self.client.app, base_url="http://127.0.0.1", client=("127.0.0.1", 12345))
        job = client.post('/api/schedules', headers={'Origin': 'http://127.0.0.1:80'},
                          json=_payload('default port')).json()
        response = client.post('/api/schedules/'+job['id']+'/run', headers={'Origin': 'http://127.0.0.1'})
        self.assertEqual(response.status_code, 202, response.text)
        scheduler._running[job['id']].thread.join(5)
        self.assertEqual(client.get('/api/schedules/'+job['id']).json()['last_result']['status'], 'ok')


if __name__ == "__main__":
    unittest.main()
