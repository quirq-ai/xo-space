"""Founder authority: a local caller or an agent token cannot impersonate a browser."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from routers.browser_guard import add_browser_write_guard
from routers.founder import require_founder, router
from services import founder_auth as auth

SECRET = "a" * 64


class FounderAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "founder-credential.json"
        self.mock = patch.object(auth, "path", return_value=self.path)
        self.mock.start()
        self.addCleanup(self.mock.stop)
        app = FastAPI()
        app.include_router(router)

        @app.post("/decision", dependencies=[Depends(require_founder)])
        async def decision():
            return {"decided": True}

        add_browser_write_guard(app, [])
        self.client = TestClient(app, base_url="https://localhost")
        self.addCleanup(self.client.close)

    def test_unenrolled_and_agent_authenticated_calls_are_refused(self):
        response = self.client.post("/decision", headers={"Authorization": "Bearer agent-session-token"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["code"], "founder_setup_required")
        auth.enroll(auth.digest(SECRET))
        self.assertEqual(self.client.post("/decision").status_code, 401)

    def test_browser_connects_only_after_local_enrollment_and_cookie_is_private(self):
        self.assertEqual(self.client.post("/api/founder/session", json={"secret": SECRET}).status_code, 401)
        auth.enroll(auth.digest(SECRET))
        self.assertNotIn(SECRET, self.path.read_text())
        response = self.client.post("/api/founder/session", json={"secret": SECRET})
        self.assertEqual((response.status_code, response.content), (204, b""))
        cookie = response.headers["set-cookie"].lower()
        for attribute in ("httponly", "secure", "samesite=strict", "path=/"):
            self.assertIn(attribute, cookie)
        self.assertEqual(self.client.post("/decision").status_code, 200)
        self.assertEqual(self.client.post("/decision", headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_missing_wrong_and_corrupt_credentials_fail_closed(self):
        auth.enroll(auth.digest(SECRET))
        self.assertEqual(self.client.post("/api/founder/session", json={"secret": "b" * 64}).status_code, 401)
        self.path.write_text(json.dumps({"version": 99, "sha256": auth.digest(SECRET)}))
        self.assertEqual(self.client.post("/decision").status_code, 503)

    def test_enrollment_is_idempotent_and_never_replaces_existing_authority(self):
        self.assertTrue(auth.enroll(auth.digest(SECRET)))
        self.assertFalse(auth.enroll(auth.digest(SECRET)))
        with self.assertRaises(auth.CredentialError):
            auth.enroll(auth.digest("b" * 64))
        auth.verify(SECRET)

    def test_pairing_page_has_no_secret_and_cannot_be_embedded(self):
        page = self.client.get("/founder")
        self.assertEqual(page.status_code, 200)
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertNotIn(SECRET, page.text)
        self.assertEqual(self.client.get("/api/founder/connect").text, page.text)
        self.assertIn('/api/founder/client.js', page.text)
        self.assertEqual(self.client.get("/api/founder/client.js").status_code, 200)

    def test_production_founder_mutations_reject_agent_bearer_without_cookie(self):
        from routers.cowork_agent import all_routers
        from routers.schedules import router as schedules
        app = FastAPI()
        for api in [*all_routers, schedules]:
            app.include_router(api)
        auth.enroll(auth.digest(SECRET))
        paths = [("POST", "/api/chat/chats/demo/messages"),
                 ("PATCH", "/api/chat/sessions/demo"),
                 ("POST", "/api/chat/review/demo/decision"),
                 ("POST", "/api/chat/intent/resume"),
                 ("PUT", "/api/chat/runtime-policy"),
                 ("POST", "/api/chat/prompt"),
                 ("POST", "/api/schedules"),
                 ("POST", "/api/deploy/demo/agent"),
                 ("PUT", "/api/runtime-config"),
                 ("POST", "/api/config/providers/demo/key")]
        with TestClient(app) as client:
            for method, path in paths:
                with self.subTest(path=path):
                    response = client.request(method, path, json={}, headers={"Authorization": "Bearer agent"})
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.json()["detail"]["code"], "founder_required")
