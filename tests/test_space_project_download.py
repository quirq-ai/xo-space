"""/api/xo-projects/{id}/download: one file's bytes as an attachment.

Driven through the router against a throwaway XO_PROJECTS_ROOT, like the
/image tests: project id + relative path only, nothing outside the project,
nothing the tree hides, any file type served as octet-stream for saving.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

BLOB = bytes(range(256)) * 8


class ProjectDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        from routers.cowork_agent.bff.xo_projects import router

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir()
        self._old_root = os.environ.get("XO_PROJECTS_ROOT")
        os.environ["XO_PROJECTS_ROOT"] = str(self.root)
        self.project = self.root / "demo"
        (self.project / "data").mkdir(parents=True)
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
        return self.client.get(
            f"/api/xo-projects/{project}/download", params={"relative_path": rel}
        )

    def test_a_file_comes_back_whole_as_an_attachment(self) -> None:
        (self.project / "data" / "dump.bin").write_bytes(BLOB)
        res = self.get("data/dump.bin")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, BLOB)
        self.assertEqual(res.headers["content-type"], "application/octet-stream")
        self.assertEqual(res.headers["content-length"], str(len(BLOB)))
        self.assertEqual(res.headers["x-content-type-options"], "nosniff")
        self.assertTrue(res.headers["content-security-policy"].startswith("sandbox"))
        self.assertEqual(
            res.headers["content-disposition"],
            "attachment; filename*=UTF-8''dump.bin",
        )

    def test_any_type_downloads_even_past_the_preview_cap(self) -> None:
        from routers.cowork_agent.bff.xo_projects import PREVIEW_MAX_BYTES

        big = b"x" * (PREVIEW_MAX_BYTES * 2 + 7)
        (self.project / "report.md").write_bytes(big)
        (self.project / "page.html").write_text("<script>alert(1)</script>")
        res = self.get("report.md")
        self.assertEqual(res.content, big)
        res = self.get("page.html")
        self.assertEqual(res.headers["content-type"], "application/octet-stream")

    def test_nothing_outside_the_project_is_served(self) -> None:
        outside = Path(self._tmp.name) / "secret.txt"
        outside.write_text("s")
        (self.project / "link.txt").symlink_to(outside)
        for rel in ("../secret.txt", "/etc/passwd", "data/../../secret.txt", "link.txt", ""):
            res = self.get(rel)
            self.assertEqual(res.status_code, 400, rel)
            self.assertEqual(res.json()["detail"]["code"], "invalid_relative_path")

    def test_what_the_tree_hides_is_not_served(self) -> None:
        (self.project / ".env").write_text("TOKEN=x")
        (self.project / ".xo").mkdir()
        (self.project / ".xo" / "project.json").write_text("{}")
        (self.project / "data" / "a.swp").write_text("x")
        (self.project / "notes.txt").symlink_to(self.project / ".env")
        for rel in (".env", ".xo/project.json", "data/a.swp", "notes.txt"):
            res = self.get(rel)
            self.assertEqual(res.status_code, 404, rel)
            self.assertEqual(res.json()["detail"]["code"], "file_not_found")

    def test_missing_file_project_or_folder_is_404(self) -> None:
        self.assertEqual(self.get("gone.csv").json()["detail"]["code"], "file_not_found")
        self.assertEqual(self.get("data").status_code, 404)
        self.assertEqual(
            self.get("x.csv", project="nope").json()["detail"]["code"], "project_not_found"
        )

    def test_an_oversized_file_is_refused(self) -> None:
        from routers.cowork_agent.bff import xo_projects

        (self.project / "big.bin").write_bytes(BLOB)
        old = xo_projects.DOWNLOAD_MAX_BYTES
        xo_projects.DOWNLOAD_MAX_BYTES = len(BLOB) - 1
        try:
            res = self.get("big.bin")
        finally:
            xo_projects.DOWNLOAD_MAX_BYTES = old
        self.assertEqual(res.status_code, 413)
        self.assertEqual(res.json()["detail"]["code"], "download_too_large")


if __name__ == "__main__":
    unittest.main()
