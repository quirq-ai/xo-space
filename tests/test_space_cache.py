"""The Space folder is served with Cache-Control: no-cache (routers/space.py).

A browser keeps its copy of every stylesheet and module but revalidates it
on each load, sending If-None-Match against the ETag and getting a 304 with
no body while the file is unchanged, and the new bytes the moment it
changes. That replaced the ?v= cache stamps the UI used to carry on every
import and link, so none may remain under space_ui."""
from __future__ import annotations

import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.space import SPACE_DIR, mount_space

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "space_ui"
# Text files under space_ui that could carry a stamp; the fonts are binary.
TEXT_SUFFIXES = {".html", ".js", ".css", ".md", ".txt", ".json", ".svg"}


@unittest.skipUnless(SPACE_DIR.exists(), "Space folder not present")
class SpaceCacheHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app = FastAPI()
        mount_space(app)
        cls.client = TestClient(app)

    def test_module_is_served_with_no_cache(self) -> None:
        res = self.client.get("/space/js/shell.js")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["cache-control"], "no-cache")
        self.assertIn("etag", res.headers)
        self.assertIn("registerView", res.text)

    def test_index_html_is_served_with_no_cache(self) -> None:
        res = self.client.get("/space/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["cache-control"], "no-cache")
        self.assertIn("etag", res.headers)
        self.assertIn("<title>XO Space</title>", res.text)

    def test_unchanged_file_answers_304_with_no_cache(self) -> None:
        for path in ("/space/js/shell.js", "/space/css/base.css", "/space/"):
            with self.subTest(path=path):
                first = self.client.get(path)
                self.assertEqual(first.status_code, 200)
                again = self.client.get(path, headers={"If-None-Match": first.headers["etag"]})
                self.assertEqual(again.status_code, 304)
                self.assertEqual(again.content, b"")
                # the 304 carries the header too, so the browser keeps revalidating
                self.assertEqual(again.headers["cache-control"], "no-cache")
                self.assertEqual(again.headers["etag"], first.headers["etag"])

    def test_a_different_etag_gets_the_body_again(self) -> None:
        res = self.client.get("/space/js/shell.js", headers={"If-None-Match": '"stale"'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["cache-control"], "no-cache")
        self.assertTrue(res.content)

    def test_missing_file_is_a_404(self) -> None:
        self.assertEqual(self.client.get("/space/js/no-such-module.js").status_code, 404)


class NoCacheStampsTests(unittest.TestCase):
    def test_no_cache_stamp_remains_under_space_ui(self) -> None:
        stamped = []
        for path in sorted(UI.rglob("*")):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8")
            if "?v=" in text:
                stamped.append(str(path.relative_to(ROOT)))
        self.assertEqual(stamped, [], "cache stamps left behind; the mount sends no-cache instead")

    def test_index_html_carries_no_import_map(self) -> None:
        html = (UI / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("importmap", html)
        self.assertIn('<script type="module" src="js/shell.js"></script>', html)


if __name__ == "__main__":
    unittest.main()
