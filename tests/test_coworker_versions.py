"""GET /api/coworker/xo-projects/{id}/version: a file's exact bytes at a
commit (services/coworker/versions), for the coworker UI's Changes view."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
#: Bytes text decoding would mangle: NUL, CRLF, invalid UTF-8.
PNG_V1 = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) + b"\xff\xfe one"
PNG_V2 = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(255, -1, -1)) + b" two"


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class VersionRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        from routers.cowork_agent.coworker import coworker_routers

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.project = self.base / "projects" / "demo"
        (self.project / "img" / "deep").mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.base / "projects"),
                                           "QUIRQ_STATE_ROOT": str(self.base / "state"), **GIT_ENV})
        env.start()
        self.addCleanup(env.stop)
        sh(self.project, "init", "-q", "-b", "main")
        (self.project / ".gitignore").write_text(".xo/\n")
        (self.project / "img" / "deep" / "logo.png").write_bytes(PNG_V1)
        (self.project / "data.bin").write_bytes(PNG_V1[::-1])
        (self.project / ".secret.png").write_bytes(PNG_V1)
        sh(self.project, "add", ".")
        sh(self.project, "commit", "-q", "-m", "base")
        self.base_commit = sh(self.project, "rev-parse", "HEAD").strip()
        sh(self.project, "worktree", "add", "-q", ".xo/worktrees/t1", "-b", "xo/t1")
        self.tree = self.project / ".xo" / "worktrees" / "t1"
        app = FastAPI()
        for router in coworker_routers:
            app.include_router(router)
        self.client = TestClient(app, base_url="http://localhost")

    def get(self, rel: str, commit: str, **params):
        return self.client.get("/api/coworker/xo-projects/demo/version",
                               params={"relative_path": rel, "commit": commit, **params})

    def test_a_changed_image_before_and_after_in_a_thread_s_sub_folder(self) -> None:
        (self.tree / "img" / "deep" / "logo.png").write_bytes(PNG_V2)
        sh(self.tree, "commit", "-q", "-am", "new logo")
        rel = ".xo/worktrees/t1/img/deep/logo.png"
        before = self.get(rel, self.base_commit)
        self.assertEqual(before.status_code, 200, before.text)
        self.assertEqual(before.content, PNG_V1, "bytes exactly, never through text decoding")
        self.assertEqual(before.headers["content-type"], "image/png")
        self.assertTrue(before.headers["content-disposition"].startswith("inline"))
        self.assertIn("immutable", before.headers["cache-control"])
        self.assertEqual(before.headers["x-xo-commit"], self.base_commit)
        head = self.get(rel, "HEAD")
        self.assertEqual(head.content, PNG_V2, "HEAD is the worktree's own")
        self.assertIn("no-cache", head.headers["cache-control"])
        self.assertEqual(self.get(rel, self.base_commit[:9]).content, PNG_V1)

    def test_a_deleted_or_renamed_file_and_a_non_image(self) -> None:
        sh(self.tree, "mv", "img/deep/logo.png", "img/brand.png")
        (self.tree / "data.bin").unlink()
        got = self.get(".xo/worktrees/t1/img/brand.png", self.base_commit, commit_path="img/deep/logo.png")
        self.assertEqual((got.status_code, got.content), (200, PNG_V1))
        gone = self.get(".xo/worktrees/t1/data.bin", self.base_commit)
        self.assertEqual((gone.status_code, gone.content), (200, PNG_V1[::-1]), "the before of a deletion")
        self.assertEqual(gone.headers["content-type"], "application/octet-stream")
        self.assertTrue(gone.headers["content-disposition"].startswith("attachment"))
        self.assertEqual(self.get("img/deep/logo.png", "HEAD").content, PNG_V1, "the project's own checkout")

    def test_refusals(self) -> None:
        outside = self.base / "outside.png"
        outside.write_bytes(PNG_V1)
        (self.project / "link.png").symlink_to(outside)
        cases = [
            (self.get("link.png", "HEAD"), 403, "outside_project"),
            (self.get("../outside.png", "HEAD"), 400, "invalid_relative_path"),
            (self.get("img/deep/logo.png", "main~1;rm"), 400, "invalid_version_request"),
            (self.get("img/deep/logo.png", "HEAD", commit_path="../x.png"), 400, "invalid_version_request"),
            (self.get("img/deep/logo.png", "f" * 40), 404, "version_not_found"),
            (self.get("img/nope.png", "HEAD"), 404, "version_not_found"),
            (self.get(".secret.png", "HEAD"), 404, "file_not_found"),
            (self.client.get("/api/coworker/xo-projects/nope/version",
                             params={"relative_path": "a.png", "commit": "HEAD"}), 404, "project_not_found"),
        ]
        for res, status, code in cases:
            with self.subTest(code=code, status=status):
                self.assertEqual((res.status_code, res.json()["detail"]["code"]), (status, code), res.text)
        self.assertEqual(self.get(".secret.png", "HEAD", hidden="true").content, PNG_V1)
        with mock.patch("routers.cowork_agent.coworker.versions.IMAGE_MAX_BYTES", 10):
            res = self.get("img/deep/logo.png", "HEAD")
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (413, "image_too_large"))
        evil = TestClient(self.client.app, base_url="http://attacker.example:5002")
        res = evil.get("/api/coworker/xo-projects/demo/version",
                       params={"relative_path": "img/deep/logo.png", "commit": "HEAD"})
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
