"""Workspace branding persists validated changes without touching real state."""

from __future__ import annotations

import builtins
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from routers.space import router
from services import branding


def image_bytes(format: str = "PNG", size: tuple[int, int] = (8, 8), color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color=color).save(output, format=format)
    return output.getvalue()


class BrandingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "settings" / "branding.json"
        env = patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app, base_url="https://workspace.example")
        self.addCleanup(self.client.close)

    def save(self, name="My workspace", logo=None, remove_logo=False, **kwargs):
        files = {"logo": ("../../outside.svg", logo, "image/png")} if logo is not None else None
        return self.client.put("/space/branding", data={"name": name, "remove_logo": str(remove_logo).lower()}, files=files, **kwargs)

    def test_default_reads_do_not_create_state_and_missing_logo_is_404(self):
        response = self.client.get("/space/branding")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"name": "Space", "logo_url": None})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/space/branding/logo").status_code, 404)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_name_is_trimmed_and_survives_a_fresh_read(self):
        response = self.save(name="  Acme Studio  ")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"name": "Acme Studio", "logo_url": None})
        self.assertEqual(branding.get_branding(), response.json())
        self.assertEqual(json.loads(self.path.read_text())["name"], "Acme Studio")

    def test_all_supported_image_formats_use_verified_mime_and_ignore_filename(self):
        for format, media_type in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
            with self.subTest(format=format):
                content = image_bytes(format)
                saved = self.save(logo=content)
                self.assertEqual(saved.status_code, 200, saved.text)
                public = saved.json()
                self.assertTrue(public["logo_url"].startswith("/space/branding/logo?v="))
                self.assertNotIn("data", public)
                response = self.client.get(public["logo_url"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, content)
                self.assertEqual(response.headers["content-type"], media_type)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["cache-control"], "no-cache")
                self.assertEqual(response.headers["etag"].strip('"'), public["logo_url"].split("=")[1])
        self.assertFalse((self.root / "outside.svg").exists())

    def test_rename_preserves_logo_replacement_changes_version_and_removal_persists(self):
        first = self.save(logo=image_bytes()).json()
        renamed = self.save(name="New name").json()
        self.assertEqual(renamed["logo_url"], first["logo_url"])
        replaced = self.save(name="New name", logo=image_bytes(color="blue")).json()
        self.assertNotEqual(replaced["logo_url"], first["logo_url"])
        removed = self.save(name="New name", remove_logo=True)
        self.assertEqual(removed.json(), {"name": "New name", "logo_url": None})
        self.assertEqual(branding.get_branding(), removed.json())
        self.assertIsNone(json.loads(self.path.read_text())["logo"])
        self.assertEqual(self.client.get("/space/branding/logo").status_code, 404)

    def test_invalid_names_do_not_replace_saved_identity(self):
        original = self.save().json()
        for value in ("", "  ", "x" * 81, "Acme\x00Studio", "Two\nLines"):
            with self.subTest(value=value):
                response = self.save(name=value)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(branding.get_branding(), original)
        self.assertEqual(self.save(name="x" * 80).status_code, 200)

    def test_bad_and_unsupported_uploads_do_not_partially_save_the_name(self):
        original = self.save(logo=image_bytes()).json()
        broken_crc = bytearray(image_bytes())
        broken_crc[-13] ^= 1  # IDAT checksum, immediately before the IEND chunk.
        bad_images = (b"", b"not an image", b'<svg xmlns="http://www.w3.org/2000/svg"/>', image_bytes("GIF"), image_bytes()[:35], image_bytes("JPEG")[:-20], bytes(broken_crc))
        for content in bad_images:
            with self.subTest(content=content[:20]):
                response = self.save(name="Unsaved name", logo=content)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(branding.get_branding(), original)

    def test_oversized_images_and_conflicting_logo_actions_are_rejected(self):
        original = self.save().json()
        cases = (
            ({"logo": b"x" * (branding.MAX_LOGO_BYTES + 1)}, 413),
            ({"logo": image_bytes(size=(4097, 1))}, 422),
            ({"logo": image_bytes(), "remove_logo": True}, 422),
        )
        for arguments, expected in cases:
            with self.subTest(expected=expected):
                response = self.save(name="Unsaved name", **arguments)
                self.assertEqual(response.status_code, expected, response.text)
                self.assertEqual(branding.get_branding(), original)

    def test_failed_atomic_commit_preserves_both_previous_name_and_logo(self):
        original = self.save(logo=image_bytes()).json()
        old_bytes = self.path.read_bytes()
        with patch("services.storage.atomic_write.os.replace", side_effect=OSError("private file path")):
            response = self.save(name="Unsaved", logo=image_bytes(color="blue"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "branding_save_failed")
        self.assertNotIn("private file path", response.text)
        self.assertEqual(self.path.read_bytes(), old_bytes)
        self.assertEqual(branding.get_branding(), original)

    def test_missing_image_dependency_keeps_existing_branding_and_allows_renaming(self):
        logo = image_bytes()
        original = self.save(logo=logo).json()
        old_bytes = self.path.read_bytes()
        import_module = builtins.__import__

        def without_pillow(name, *args, **kwargs):
            if name == "PIL":
                raise ModuleNotFoundError("No module named 'PIL'", name="PIL")
            return import_module(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=without_pillow):
            response = self.save(name="Unsaved", logo=logo)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"]["code"], "branding_image_support_unavailable")
            self.assertIn("requirements", response.json()["detail"]["message"])
            self.assertEqual(self.path.read_bytes(), old_bytes)
            self.assertEqual(branding.get_branding(), original)
            renamed = self.save(name="Still available")
            self.assertEqual(renamed.status_code, 200)
            self.assertEqual(renamed.json(), {"name": "Still available", "logo_url": original["logo_url"]})
            self.assertEqual(self.client.get(original["logo_url"]).content, logo)

    def test_broken_image_dependency_is_not_misreported_as_missing_pillow(self):
        import_module = builtins.__import__

        def broken_pillow(name, *args, **kwargs):
            if name == "PIL":
                raise ModuleNotFoundError("No module named 'broken_internal'", name="broken_internal")
            return import_module(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=broken_pillow):
            with self.assertRaises(ModuleNotFoundError) as raised:
                branding.save_branding("Unsaved", b"test image bytes")
        self.assertEqual(raised.exception.name, "broken_internal")
        self.assertFalse(self.path.exists())

    def test_corrupt_or_future_settings_are_reported_and_never_overwritten(self):
        self.path.parent.mkdir(parents=True)
        cases = (
            "not JSON", "[]", '{"schema":2,"name":"Future"}',
            '{"schema":1,"name":"Existing","logo":{"media_type":"image/svg+xml","data":"bad"}}',
            '{"schema":1,"name":"Existing","logo":{"media_type":"image/png","data":"bad"}}',
        )
        for content in cases:
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertEqual(self.client.get("/space/branding").status_code, 503)
                self.assertEqual(self.save().status_code, 503)
                self.assertEqual(self.path.read_text(), content)

    def test_unreadable_settings_report_actionable_error_without_details(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("private path")):
            response = self.client.get("/space/branding")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "branding_unavailable")
        self.assertNotIn("private path", response.text)

    def test_same_origin_remote_updates_work_and_cross_origin_is_rejected(self):
        response = self.save(headers={"Origin": "https://workspace.example", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 200, response.text)
        for headers in ({"Origin": "https://attacker.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.save(name="Unsaved", headers=headers).status_code, 403)
        self.assertEqual(branding.get_branding()["name"], "My workspace")


if __name__ == "__main__":
    unittest.main()
