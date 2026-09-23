"""HTTP translation only: worker thread, guard, error mapping. Policy is tested in services."""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent import doctor
from services.doctor import leftovers

JSON = {"Content-Type": "application/json"}
PATH = "/api/doctor/runtime-leftovers/abc/move-aside"


class DoctorRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(doctor.router)
        self.client = TestClient(app)

    def test_get_returns_the_report(self) -> None:
        with patch("routers.cowork_agent.doctor.run.run_checks", return_value={"schema": 1}) as run_checks:
            response = self.client.get("/api/doctor")
        self.assertEqual((response.status_code, response.json()), (200, {"schema": 1}))
        run_checks.assert_called_once_with()

    def test_move_aside_success(self) -> None:
        result = {"moved": True, "key": "abc", "to": "/q/abc-1", "bytes": 1, "files": 1}
        with patch("routers.cowork_agent.doctor.leftovers.move_aside", return_value=result) as move:
            response = self.client.post(PATH, json={})
        self.assertEqual((response.status_code, response.json()), (200, result))
        move.assert_called_once_with("abc")

    def test_non_json_and_cross_origin_are_refused_before_the_service(self) -> None:
        with patch("routers.cowork_agent.doctor.leftovers.move_aside") as move:
            # FastAPI validates the body model before the handler runs, so a
            # non-JSON body is a 422 and the 415 guard is a second line, exactly
            # as for DELETE /api/xo-projects/{id}. Either way the service is never called.
            self.assertEqual(self.client.post(PATH, content="{}").status_code, 422)
            cross = self.client.post(PATH, json={}, headers={"Origin": "https://evil.example", **JSON})
            self.assertEqual(cross.status_code, 403)
            self.assertEqual(cross.json()["detail"]["code"], "same_origin_required")
            move.assert_not_called()

    def test_unknown_body_keys_are_rejected(self) -> None:
        with patch("routers.cowork_agent.doctor.leftovers.move_aside") as move:
            self.assertEqual(self.client.post(PATH, json={"force": True}).status_code, 422)
            move.assert_not_called()

    def test_service_errors_map_to_their_status(self) -> None:
        for code, status in (("doctor_invalid_key", 400), ("doctor_not_leftover", 409), ("doctor_move_failed", 500)):
            with self.subTest(code=code), patch("routers.cowork_agent.doctor.leftovers.move_aside",
                                                side_effect=leftovers.DoctorError(code, "message", status)):
                response = self.client.post(PATH, json={})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["detail"], {"code": code, "message": "message"})


class OneRunAtATimeTests(unittest.TestCase):
    """Requests that arrive while a run is in progress share it. A run holds a
    thread from the pool the rest of the server shares, and a large state root
    costs memory while it parses, so page opens must not multiply either."""

    def test_concurrent_requests_share_one_run(self) -> None:
        started, release = threading.Event(), threading.Event()
        calls = []

        def slow_run() -> dict:
            calls.append(1)
            started.set()
            release.wait(5)
            return {"schema": 1, "n": len(calls)}

        async def three_requests() -> list:
            first = asyncio.ensure_future(doctor.get_doctor_report())
            await asyncio.to_thread(started.wait, 5)
            rest = [asyncio.ensure_future(doctor.get_doctor_report()) for _ in range(2)]
            await asyncio.sleep(0.05)
            release.set()
            return await asyncio.gather(first, *rest)

        with patch("routers.cowork_agent.doctor.run.run_checks", side_effect=slow_run):
            reports = asyncio.run(three_requests())
        self.assertEqual(len(calls), 1)
        self.assertEqual(reports, [{"schema": 1, "n": 1}] * 3)

    def test_a_finished_run_is_not_reused(self) -> None:
        with patch("routers.cowork_agent.doctor.run.run_checks", side_effect=[{"n": 1}, {"n": 2}]) as run_checks:
            first = asyncio.run(doctor.get_doctor_report())
            second = asyncio.run(doctor.get_doctor_report())
        self.assertEqual((first, second, run_checks.call_count), ({"n": 1}, {"n": 2}, 2))

    def test_a_failed_run_is_not_reused(self) -> None:
        with patch("routers.cowork_agent.doctor.run.run_checks", side_effect=[RuntimeError("boom"), {"n": 2}]):
            with self.assertRaises(RuntimeError):
                asyncio.run(doctor.get_doctor_report())
            self.assertEqual(asyncio.run(doctor.get_doctor_report()), {"n": 2})


if __name__ == "__main__":
    unittest.main()
