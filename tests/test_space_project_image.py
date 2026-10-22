"""/api/xo-projects/{id}/image: one image's bytes for the image viewer.

Driven through the router against a throwaway XO_PROJECTS_ROOT, so the path
rules are the real ones: project id + relative path only, nothing outside the
project (not by "..", not by a link), image types only, served as they are with
headers that keep the browser from running anything.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class ProjectImageTests(unittest.TestCase):
    def setUp(self) -> None:
        from routers.cowork_agent.bff.xo_projects import router

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir()
        self._old_root = os.environ.get("XO_PROJECTS_ROOT")
        os.environ["XO_PROJECTS_ROOT"] = str(self.root)
        self.project = self.root / "demo"
        (self.project / "shots").mkdir(parents=True)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if self._old_root is None:
            os.environ.pop("XO_PROJECTS_ROOT", None)
        else:
            os.environ["XO_PROJECTS_ROOT"] = self._old_root
        self._tmp.cleanup()

    def get(self, rel: str, project: str = "demo"):
        return self.client.get(f"/api/xo-projects/{project}/image", params={"relative_path": rel})

    def test_an_image_comes_back_as_it_is_with_safe_headers(self) -> None:
        (self.project / "shots" / "home.png").write_bytes(PNG)
        res = self.get("shots/home.png")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, PNG)
        self.assertEqual(res.headers["content-type"], "image/png")
        self.assertEqual(res.headers["content-length"], str(len(PNG)))
        self.assertEqual(res.headers["x-content-type-options"], "nosniff")
        self.assertTrue(res.headers["content-security-policy"].startswith("sandbox"))
        self.assertTrue(res.headers["content-disposition"].startswith("inline"))

    def test_the_type_comes_from_the_suffix(self) -> None:
        (self.project / "a.JPG").write_bytes(b"\xff\xd8\xff")
        (self.project / "b.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        self.assertEqual(self.get("a.JPG").headers["content-type"], "image/jpeg")
        self.assertEqual(self.get("b.svg").headers["content-type"], "image/svg+xml")

    def test_anything_but_an_image_is_refused(self) -> None:
        (self.project / "notes.md").write_text("# hi")
        (self.project / "noext").write_bytes(PNG)
        for rel in ("notes.md", "noext"):
            res = self.get(rel)
            self.assertEqual(res.status_code, 415, rel)
            self.assertEqual(res.json()["detail"]["code"], "image_unsupported")

    def test_nothing_outside_the_project_is_served(self) -> None:
        outside = Path(self._tmp.name) / "secret.png"
        outside.write_bytes(PNG)
        (self.project / "link.png").symlink_to(outside)
        for rel in ("../secret.png", "/etc/x.png", "shots/../../secret.png", "link.png"):
            res = self.get(rel)
            self.assertEqual(res.status_code, 400, rel)
            self.assertEqual(res.json()["detail"]["code"], "invalid_relative_path")

    def test_missing_file_project_or_folder_is_404(self) -> None:
        (self.project / "dir.png").mkdir()
        self.assertEqual(self.get("gone.png").json()["detail"]["code"], "file_not_found")
        self.assertEqual(self.get("dir.png").status_code, 404)
        self.assertEqual(self.get("x.png", project="nope").json()["detail"]["code"], "project_not_found")

    def test_an_oversized_image_is_refused(self) -> None:
        from routers.cowork_agent.bff import xo_projects

        (self.project / "big.png").write_bytes(PNG * 4)
        old = xo_projects.IMAGE_MAX_BYTES
        xo_projects.IMAGE_MAX_BYTES = len(PNG)
        try:
            res = self.get("big.png")
        finally:
            xo_projects.IMAGE_MAX_BYTES = old
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res.json()["detail"]["code"], "image_too_large")


if __name__ == "__main__":
    unittest.main()
