"""Theme preferences persist independently of branding and the checkout."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.space import router
from services import branding, theme
from utils.commands import run_sync


class ThemeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "settings" / "theme.json"
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app, base_url="https://workspace.example")
        self.addCleanup(self.client.close)

    def save(self, value="quirq", **kwargs):
        return self.client.put("/space/theme", json={"theme": value}, **kwargs)

    def test_default_read_does_not_create_state(self):
        response = self.client.get("/space/theme")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"theme": "space"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_selected_theme_and_return_to_default_survive_fresh_reads(self):
        for selected in ("quirq", "midnight", "space"):
            with self.subTest(selected=selected):
                response = self.save(selected)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"theme": selected})
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(theme.get_theme(), response.json())
                self.assertEqual(self.client.get("/space/theme").json(), response.json())
                self.assertEqual(json.loads(self.path.read_text()), {"schema": 1, "theme": selected})

    def test_default_storage_is_outside_checkout(self):
        home = self.root / "home"
        with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": ""}), patch.object(Path, "home", return_value=home):
            response = self.save()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads((home / ".quirq/settings/theme.json").read_text()), {"schema": 1, "theme": "quirq"})
            self.assertFalse(self.path.exists())

    def test_custom_state_inside_checkout_is_ignored_by_git(self):
        checkout = self.root / "checkout"
        checkout.mkdir()
        ignore = Path(__file__).resolve().parents[1] / ".gitignore"
        (checkout / ".gitignore").write_text(ignore.read_text(), encoding="utf-8")
        initialized = run_sync(["git", "init", "--quiet"], cwd=checkout, timeout=10)
        self.assertTrue(initialized.ok, initialized.output)
        with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(checkout / "custom-state")}):
            self.assertEqual(self.save().status_code, 200)
        paths = ["custom-state/settings/theme.json", "custom-state/settings/theme.json.tmp"]
        ignored = run_sync(["git", "check-ignore", "--no-index", *paths], cwd=checkout, timeout=10)
        self.assertTrue(ignored.ok, ignored.output)
        self.assertEqual(ignored.stdout.splitlines(), paths)

    def test_invalid_themes_do_not_overwrite_the_preference(self):
        self.assertEqual(self.save().status_code, 200)
        original = self.path.read_bytes()
        for value in ("", "unknown", "QUIRQ", " quirq ", None, 1, True, [], {}):
            with self.subTest(value=value):
                self.assertEqual(self.save(value).status_code, 422)
                self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_service_input_does_not_create_state(self):
        for value in ("unknown", [], None):
            with self.subTest(value=value), self.assertRaises(theme.ThemeError) as raised:
                theme.save_theme(value)
            self.assertEqual(raised.exception.status, 422)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_or_unexpected_body_fields_are_rejected(self):
        for body in ({}, {"theme": "quirq", "name": "Unwanted rename"}, [], None):
            with self.subTest(body=body):
                self.assertEqual(self.client.put("/space/theme", json=body).status_code, 422)
        self.assertFalse(self.path.exists())

    def test_corrupt_and_future_settings_are_never_overwritten(self):
        self.path.parent.mkdir(parents=True)
        cases = (
            b"not JSON", b"\xff", b"[]", b"null", b"{}",
            b'{"schema":2,"theme":"quirq"}',
            b'{"schema":true,"theme":"quirq"}',
            b'{"schema":1.0,"theme":"quirq"}',
            b'{"schema":1,"theme":"future-theme"}',
            b'{"schema":1,"theme":[]}',
        )
        for content in cases:
            with self.subTest(content=content):
                self.path.write_bytes(content)
                self.assertEqual(self.client.get("/space/theme").status_code, 503)
                self.assertEqual(self.save().status_code, 503)
                self.assertEqual(self.path.read_bytes(), content)

    def test_failed_atomic_commit_preserves_previous_theme(self):
        self.assertEqual(self.save().status_code, 200)
        original = self.path.read_bytes()
        with patch("services.storage.atomic_write.os.replace", side_effect=OSError("private file path")):
            response = self.save("space")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "theme_save_failed")
        self.assertNotIn("private file path", response.text)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(theme.get_theme(), {"theme": "quirq"})

    def test_unreadable_settings_report_error_without_private_details(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("private path")):
            response = self.client.get("/space/theme")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "theme_unavailable")
        self.assertNotIn("private path", response.text)

    def test_same_origin_updates_work_and_cross_origin_cannot_change_saved_theme(self):
        response = self.save(headers={"Origin": "https://workspace.example", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 200)
        for headers in ({"Origin": "https://attacker.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.save("space", headers=headers).status_code, 403)
        self.assertEqual(theme.get_theme(), {"theme": "quirq"})

    def test_theme_and_branding_changes_preserve_each_other(self):
        # Pre-existing branding can be read without an image decoder. Use a
        # minimal PNG fixture to exercise logo preservation as well as the name.
        logo = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jV1sAAAAASUVORK5CYII=")
        branding_path = self.root / "settings" / "branding.json"
        branding_path.parent.mkdir(parents=True)
        branding_path.write_text(json.dumps({
            "schema": 1,
            "name": "My workspace",
            "logo": {"media_type": "image/png", "data": base64.b64encode(logo).decode("ascii")},
        }))
        original = branding_path.read_bytes()
        public = branding.get_branding()
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(branding_path.read_bytes(), original)
        self.assertEqual(branding.get_branding(), public)
        self.assertEqual(self.client.get(public["logo_url"]).content, logo)
        saved_theme = self.path.read_bytes()
        renamed = self.client.put("/space/branding", data={"name": "Renamed workspace"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["logo_url"], public["logo_url"])
        self.assertEqual(self.path.read_bytes(), saved_theme)
        self.assertEqual(self.save("space").status_code, 200)
        self.assertEqual(branding.get_branding()["name"], "Renamed workspace")
        self.assertEqual(self.client.get(public["logo_url"]).content, logo)


if __name__ == "__main__":
    unittest.main()
