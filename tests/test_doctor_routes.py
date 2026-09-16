"""HTTP translation only: worker thread, guard, error mapping. Policy is tested in services."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
