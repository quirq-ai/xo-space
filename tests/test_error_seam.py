"""The one HTTP seam for service failures (``routers/errors.py``).

A route, a dependency or a service raises :class:`services.errors.ServiceError`
(or a subclass) and the app handler answers with the raiser's status and
``{"detail": {"code", "message"}}``; ``log`` goes to the server log and never
to the wire. There is no per-router mapping to test: every router's error
behaviour is this handler plus the status each raise site chose.
"""

from __future__ import annotations

import logging
import unittest

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from routers.errors import install_service_errors
from services.errors import Conflict, NotFound, ServiceError, Unavailable


def _app() -> TestClient:
    router = APIRouter()

    @router.get("/teapot")
    def teapot() -> dict:
        raise ServiceError("short_and_stout", "I am a teapot.", 418, log="secret path /var/lib/teapot")

    @router.get("/missing")
    def missing() -> dict:
        raise NotFound("thing_not_found", "Thing not found.")

    @router.get("/conflict")
    def conflict() -> dict:
        raise Conflict("state_refuses", "Not now.")

    @router.get("/down")
    def down() -> dict:
        raise Unavailable("dependency_down", "The dependency is missing.")

    @router.get("/plain")
    def plain() -> dict:
        raise ServiceError(None, "no such job: j1", 404)

    def gate() -> None:
        raise ServiceError("gated", "Closed.", 403)

    @router.get("/gated", dependencies=[Depends(gate)])
    def gated() -> dict:
        return {"open": True}

    app = FastAPI()
    app.include_router(router)
    install_service_errors(app)
    return TestClient(app)


class ServiceErrorSeamTests(unittest.TestCase):
    def test_status_code_and_message_reach_the_wire_and_the_log_does_not(self) -> None:
        with self.assertLogs("xo_space.errors", level="WARNING") as logged:
            r = _app().get("/teapot")
        self.assertEqual(r.status_code, 418)
        self.assertEqual(r.json(), {"detail": {"code": "short_and_stout", "message": "I am a teapot."}})
        self.assertNotIn("secret path", r.text)
        self.assertNotIn("/var/lib/teapot", r.text)
        self.assertTrue(any("secret path /var/lib/teapot" in line for line in logged.output))

    def test_the_subclasses_carry_their_status(self) -> None:
        c = _app()
        self.assertEqual((c.get("/missing").status_code, c.get("/missing").json()["detail"]["code"]), (404, "thing_not_found"))
        self.assertEqual((c.get("/conflict").status_code, c.get("/conflict").json()["detail"]["code"]), (409, "state_refuses"))
        self.assertEqual((c.get("/down").status_code, c.get("/down").json()["detail"]["code"]), (503, "dependency_down"))

    def test_an_error_without_a_code_answers_the_bare_message(self) -> None:
        r = _app().get("/plain")
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "no such job: j1"}))

    def test_a_dependency_raising_on_the_request_path_is_answered_the_same_way(self) -> None:
        r = _app().get("/gated")
        self.assertEqual((r.status_code, r.json()), (403, {"detail": {"code": "gated", "message": "Closed."}}))

    def test_an_error_without_log_writes_nothing(self) -> None:
        logger = logging.getLogger("xo_space.errors")
        with self.assertNoLogs(logger, level="WARNING"):
            _app().get("/missing")

    def test_the_real_app_installs_the_handler(self) -> None:
        from server import app
        from routers.errors import service_error_response
        handlers = getattr(app, "exception_handlers", {})
        self.assertIs(handlers.get(ServiceError), service_error_response)


if __name__ == "__main__":
    unittest.main()
