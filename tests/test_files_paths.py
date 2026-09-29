"""Path containment for ``/api/files/*`` (routers/cowork_agent/files.py).

The routes promise that every path stays inside the user's home directory;
an upload may also target the xo-projects root, which a Docker install keeps
outside home (``/workspace/xo-projects`` with home ``/root``). Containment is
decided on resolved paths, never on string prefixes, so a sibling such as
``/home/user-shared`` is outside ``/home/user``, and an upload keeps only the
file's own name. POSIX only (symlinks, the routers import chain). Every path
is a temp directory.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

try:
    from routers.cowork_agent.files import router
except ImportError as exc:  # pragma: no cover - platform gate
    raise unittest.SkipTest(f"routers package needs POSIX: {exc}") from exc


class FilePathContainmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.home = root / "home" / "user"
        self.sibling = root / "home" / "user-shared"  # its path starts with the home path
        self.projects = root / "workspace" / "xo-projects"  # outside home, as in Docker
        self.outside = root / "elsewhere"
        for directory in (self.home, self.sibling, self.projects, self.outside):
            directory.mkdir(parents=True)
        self._env = patch.dict(os.environ, {"HOME": str(self.home), "XO_PROJECTS_ROOT": str(self.projects)})
        self._env.start()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def upload(self, name: str, workspace: Path | str | None = None, data: bytes = b"payload"):
        form = {} if workspace is None else {"workspace": str(workspace)}
        return self.client.post("/api/files/upload", files={"file": (name, data)}, data=form)

    def files_under(self, directory: Path) -> list[Path]:
        return sorted(p for p in directory.rglob("*") if p.is_file())

    # ── upload ────────────────────────────────────────────────────────────

    def test_upload_still_lands_in_home_the_projects_root_and_the_default(self) -> None:
        for workspace, expected_dir in ((self.home / "notes", self.home / "notes"),
                                        (self.projects / "app", self.projects / "app"),
                                        (None, self.home / "uploads")):
            with self.subTest(workspace=workspace):
                response = self.upload("report.txt", workspace)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(Path(response.json()["path"]), expected_dir / "report.txt")
                self.assertEqual((expected_dir / "report.txt").read_bytes(), b"payload")

    def test_upload_outside_home_and_the_projects_root_is_refused(self) -> None:
        link = self.home / "linked"
        link.symlink_to(self.outside, target_is_directory=True)
        for workspace in (self.outside, self.sibling, link):
            with self.subTest(workspace=workspace):
                self.assertEqual(self.upload("report.txt", workspace).status_code, 403)
        self.assertEqual(self.files_under(self.outside), [])
        self.assertEqual(self.files_under(self.sibling), [])

    def test_upload_keeps_only_the_file_name(self) -> None:
        for name in ("../escape.txt", "../../escape.txt", str(self.outside / "escape.txt"), "sub/dir/nested.txt"):
            with self.subTest(name=name):
                response = self.upload(name, self.home / "inbox")
                self.assertEqual(response.status_code, 200, response.text)
                saved = Path(response.json()["path"])
                self.assertEqual(saved.parent, self.home / "inbox")
                self.assertEqual(saved.name, Path(name).name)
        self.assertEqual(self.files_under(self.outside), [])
        self.assertFalse((self.home / "escape.txt").exists())

    def test_an_empty_or_dot_file_name_becomes_upload(self) -> None:
        for name in ("..", "."):
            with self.subTest(name=name):
                response = self.upload(name, self.home / "inbox")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(Path(response.json()["path"]).parent, self.home / "inbox")

    # ── the other routes: containment, not string prefixes ────────────────

    def test_a_sibling_whose_name_extends_home_is_outside(self) -> None:
        secret = self.sibling / "secret.txt"
        secret.write_text("not yours", encoding="utf-8")
        requests = [
            ("/api/files/list-directory", {"path": str(self.sibling)}),
            ("/api/files/content", {"path": str(secret)}),
            ("/api/files/content-binary", {"path": str(secret)}),
            ("/api/files/save", {"path": str(self.sibling / "planted.txt"), "content": "x"}),
            ("/api/files/mkdir", {"path": str(self.sibling / "newdir")}),
            ("/api/files/mkdir", {"path": str(self.home / "copy-target"), "files": [str(secret)]}),
        ]
        for path, body in requests:
            with self.subTest(route=path, body=body):
                self.assertEqual(self.client.post(path, json=body).status_code, 403)
        self.assertFalse((self.sibling / "planted.txt").exists())
        self.assertFalse((self.sibling / "newdir").exists())
        self.assertFalse((self.home / "copy-target").exists())

    def test_home_itself_and_paths_inside_it_still_work(self) -> None:
        (self.home / "notes.md").write_text("hello", encoding="utf-8")
        self.assertEqual(self.client.post("/api/files/list-directory", json={"path": str(self.home)}).status_code, 200)
        self.assertEqual(self.client.post("/api/files/content", json={"path": str(self.home / "notes.md")}).json()["content"], "hello")
        saved = self.client.post("/api/files/save", json={"path": str(self.home / "a" / "b.txt"), "content": "x"})
        self.assertEqual(saved.status_code, 200, saved.text)
        made = self.client.post("/api/files/mkdir", json={"path": str(self.home / "fresh")})
        self.assertEqual(made.status_code, 200, made.text)


if __name__ == "__main__":
    unittest.main()
