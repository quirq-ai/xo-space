from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent import git_snapshot


ROOT = Path(__file__).resolve().parents[1]


def _git(pdir: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(pdir), "-c", "user.email=t@t", "-c", "user.name=t",
         *args],
        check=True, capture_output=True,
    )


class GitSnapshotServiceTests(unittest.TestCase):
    """The service against a real throwaway repository."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls._tmp.name) / "proj"
        cls.repo.mkdir()
        r = cls.repo
        _git(r, "init", "-q")
        (r / "a.py").write_text("one\n")
        (r / "docs").mkdir()
        (r / "docs" / "guide.md").write_text("# guide\n")
        _git(r, "add", "-A")
        _git(r, "commit", "-q", "-m", "first")
        (r / "a.py").write_text("one\ntwo\n")
        (r / "b.txt").write_text("new file\n")
        (r / "docs" / "guide.md").unlink()
        _git(r, "add", "-A")
        _git(r, "commit", "-q", "-m", "second: modify a, add b, drop guide")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _shas(self) -> list[dict]:
        commits = git_snapshot.list_commits(self.repo)
        self.assertEqual(len(commits), 2)
        return commits

    def test_list_commits_newest_first_with_counts(self) -> None:
        newest, oldest = self._shas()
        self.assertEqual(newest["subject"], "second: modify a, add b, drop guide")
        self.assertEqual(oldest["subject"], "first")
        self.assertEqual(newest["files_changed"], 3)
        self.assertEqual(oldest["files_changed"], 2)
        self.assertTrue(newest["date"])  # iso-strict

    def test_snapshot_tree_and_touched(self) -> None:
        newest, oldest = self._shas()

        snap = git_snapshot.commit_snapshot(self.repo, newest["sha"])
        self.assertEqual(
            sorted(e["path"] for e in snap["tree"]), ["a.py", "b.txt"]
        )
        self.assertEqual(snap["touched"], {"a.py": "M", "b.txt": "A"})
        self.assertEqual(snap["deleted"], ["docs/guide.md"])
        self.assertFalse(snap["truncated"])
        self.assertEqual(snap["total_files"], 2)
        # sizes come from ls-tree, so they match the committed blobs
        by = {e["path"]: e["size"] for e in snap["tree"]}
        self.assertEqual(by["a.py"], len("one\ntwo\n"))

        # the FIRST commit still works: --root diffs against the empty tree
        first = git_snapshot.commit_snapshot(self.repo, oldest["sha"])
        self.assertEqual(
            sorted(first["touched"]), ["a.py", "docs/guide.md"]
        )
        self.assertEqual(set(first["touched"].values()), {"A"})
        self.assertEqual(
            sorted(e["path"] for e in first["tree"]), ["a.py", "docs/guide.md"]
        )

    def test_file_at_commit_is_commit_pinned(self) -> None:
        newest, oldest = self._shas()
        old = git_snapshot.file_at_commit(
            self.repo, oldest["sha"], "a.py", max_bytes=1024
        )
        new = git_snapshot.file_at_commit(
            self.repo, newest["sha"], "a.py", max_bytes=1024
        )
        self.assertEqual(old["content"], "one\n")
        self.assertEqual(new["content"], "one\ntwo\n")
        # deleted at newest, present at oldest
        self.assertIsNone(git_snapshot.file_at_commit(
            self.repo, newest["sha"], "docs/guide.md", max_bytes=64
        ))
        self.assertIsNotNone(git_snapshot.file_at_commit(
            self.repo, oldest["sha"], "docs/guide.md", max_bytes=64
        ))

    def test_churn_carries_line_counts_per_file(self) -> None:
        newest, oldest = self._shas()
        snap = git_snapshot.commit_snapshot(self.repo, newest["sha"])
        churn = snap["churn"]
        # a.py went from one line to two: +1, -0
        self.assertEqual(churn["a.py"], {"added": 1, "deleted": 0})
        # b.txt is new: one added line
        self.assertEqual(churn["b.txt"], {"added": 1, "deleted": 0})
        # churn is scoped to files present in this commit's tree, so the
        # deleted guide does not appear beside files the map can draw
        self.assertNotIn("docs/guide.md", churn)
        self.assertEqual(set(churn), set(snap["touched"]))

    def test_binary_churn_is_none_not_zero(self) -> None:
        """"cannot count lines" and "changed no lines" are different facts.

        Built in its own repository: the shared fixture's history is
        asserted commit-for-commit by every other test here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            r = Path(tmp) / "bin"
            r.mkdir()
            _git(r, "init", "-q")
            (r / "blob.bin").write_bytes(bytes(range(256)) * 8)
            (r / "notes.txt").write_text("hello\n")
            _git(r, "add", "-A")
            _git(r, "commit", "-q", "-m", "add a binary and a text file")
            sha = git_snapshot.list_commits(r, limit=1)[0]["sha"]
            churn = git_snapshot.commit_snapshot(r, sha)["churn"]
            self.assertEqual(churn["blob.bin"], {"added": None, "deleted": None})
            # the text file beside it still counts normally
            self.assertEqual(churn["notes.txt"], {"added": 1, "deleted": 0})

    def test_hostile_input_never_reaches_git(self) -> None:
        self.assertIsNone(git_snapshot.normalize_sha("HEAD"))
        self.assertIsNone(git_snapshot.normalize_sha("--exec=x"))
        self.assertIsNone(git_snapshot.normalize_sha(""))
        newest = self._shas()[0]["sha"]
        for bad in ("../../etc/passwd", "/abs", "a/../b", ""):
            with self.assertRaises(ValueError):
                git_snapshot.file_at_commit(self.repo, newest, bad, max_bytes=8)

    def test_commits_on_day_filters_by_author_day(self) -> None:
        newest = self._shas()[0]
        day = newest["date"][:10]
        on_day = git_snapshot.commits_on_day(self.repo, day)
        # both fixture commits were authored in the same test run
        self.assertEqual(len(on_day), 2)
        self.assertEqual(on_day[0]["sha"], newest["sha"])
        self.assertEqual(git_snapshot.commits_on_day(self.repo, "1999-01-01"), [])
        self.assertIsNone(git_snapshot.commits_on_day(self.repo, "not-a-day"))
        self.assertTrue(git_snapshot.valid_day("2026-08-20"))
        self.assertFalse(git_snapshot.valid_day("--since=x"))

    def test_non_repo_directory_is_none_not_parent_history(self) -> None:
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir(exist_ok=True)
        self.assertIsNone(git_snapshot.list_commits(plain))
        self.assertIsNone(git_snapshot.commit_snapshot(plain, "a" * 40))


class SnapshotWiringTests(unittest.TestCase):
    """The UI and router are wired the way the feature needs."""

    def test_snapshot_view_registered_hidden_from_nav(self) -> None:
        app = (ROOT / "space_ui" / "js" / "app.js").read_text(encoding="utf-8")
        index = (ROOT / "space_ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn("import snapshotView from './views/snapshot.js?v=", app)
        self.assertIn("registerView(snapshotView);", app)
        self.assertIn('href="css/snapshot.css?v=', index)
        src = (ROOT / "space_ui" / "js" / "views" / "snapshot.js").read_text(
            encoding="utf-8"
        )
        head = src.split("export default", 1)[1]
        contract = head[: head.index("mount(")]
        self.assertIn("nav:false", contract)
        # the snapshot belongs to the Timeline: its tab stays highlighted
        # and the back button returns there
        self.assertIn("parent:'time'", contract)
        self.assertIn("go('time')", src)

    def test_timeline_dots_resolve_days_and_open_snapshots(self) -> None:
        atlas = (ROOT / "space_ui" / "js" / "views" / "atlas.js").read_text(
            encoding="utf-8"
        )
        # a commit-day dot opens the chooser, which resolves the day to shas
        self.assertIn("openCommitDay(d,", atlas)
        self.assertIn("/commits?day=", atlas)
        self.assertIn("space:show-commit", atlas)
        self.assertIn("go('snapshot')", atlas)
        # the graph panel no longer carries the entry point
        self.assertNotIn("commitSectionHTML", atlas)
        self.assertNotIn("pcommit", atlas)
        snapshot = (ROOT / "space_ui" / "js" / "views" / "snapshot.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("space:show-commit", snapshot)
        # a clicked file opens the shared previewer pinned to the commit
        self.assertIn("space:preview-file", snapshot)
        self.assertIn("ref:cur.sha", snapshot)

    def test_snapshot_speaks_space_walk_citymap_grammar(self) -> None:
        """The renderer is a deliberate port of mindwalk's citymap; these
        literals ARE the contract — layout constants from
        internal/citymap/builder.go, palette from web/src/scene."""
        src = (ROOT / "space_ui" / "js" / "views" / "snapshot.js").read_text(
            encoding="utf-8"
        )
        # builder.go: the 120-unit plain, 0.08 inset streets, aspect cap 40
        self.assertIn("const WORLD=120", src)
        self.assertIn("INSET=0.08", src)
        self.assertIn("ASPECT_CAP=40", src)
        # fileWeight's byte fallback: sqrt(max(bytes/4096, 16))
        self.assertIn("/4096,16", src)
        # CityScene: plate shading, unvisited tile + FNV-1a jitter
        self.assertIn("#161a20", src.lower())
        self.assertIn("16777619", src)
        # sceneUtils touch lattice: edit / hit / read
        self.assertIn("'#a8d94f'", src)
        self.assertIn("'#a8a24e'", src)
        self.assertIn("'#9dc0e8'", src)
        # dirLabels: LOD threshold and label budget
        self.assertIn("LABEL_MIN_SUBTREE_PX=60", src)
        self.assertIn("LABEL_BUDGET=120", src)

    def test_three_is_vendored_and_self_contained(self) -> None:
        """The browser imports three directly — so every specifier it
        reaches for must resolve on disk, with no bundler and no CDN."""
        vendor = ROOT / "space_ui" / "vendor"
        for name in ("three.module.min.js", "three.core.min.js",
                     "OrbitControls.js"):
            self.assertTrue((vendor / name).is_file(), f"missing {name}")
        orbit = (vendor / "OrbitControls.js").read_text(encoding="utf-8")
        # a bare "three" specifier would 404 in a browser with no importmap
        self.assertNotIn("from 'three'", orbit)
        self.assertIn("from './three.module.min.js'", orbit)
        # three.module re-exports its core chunk; that file must be here too
        module = (vendor / "three.module.min.js").read_text(encoding="utf-8")
        self.assertIn("three.core.min.js", module)

    def test_snapshot_renders_in_3d_and_loads_three_lazily(self) -> None:
        src = (ROOT / "space_ui" / "js" / "views" / "snapshot.js").read_text(
            encoding="utf-8"
        )
        # dynamic import: nobody who never opens a snapshot pays for three
        self.assertIn("import('../../vendor/three.module.min.js')", src)
        self.assertNotIn("import * as THREE from", src)
        self.assertIn("WebGLRenderer", src)
        self.assertIn("OrbitControls", src)
        # light is data: the terrain is the one emissive material, and
        # vertexColors must stay off (BoxGeometry carries no colour
        # attribute — instanceColor does the work, see setColorAt)
        self.assertIn("MeshBasicMaterial({toneMapped:false})", src)
        self.assertNotIn("vertexColors:true", src)

    def test_height_encodes_depth_of_change(self) -> None:
        src = (ROOT / "space_ui" / "js" / "views" / "snapshot.js").read_text(
            encoding="utf-8"
        )
        # churn drives terrain height, with both height modes offered
        self.assertIn("function churnHeight(", src)
        self.assertIn("CHURN_GAMMA", src)
        self.assertIn('data-h="churn"', src)
        self.assertIn('data-h="size"', src)
        # size mode is Space Walk's own locHeights fallback
        self.assertIn("LOC_HEIGHT_GAMMA=2.2", src)
        self.assertIn("LOC_MAX_H=16", src)

    def test_previewer_forwards_the_ref(self) -> None:
        preview = (ROOT / "space_ui" / "js" / "core" / "preview.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("'&ref='+encodeURIComponent(current.ref)", preview)

    def test_routes_are_registered(self) -> None:
        from routers.cowork_agent.bff import bff_routers

        paths = {r.path for router in bff_routers for r in router.routes}
        self.assertIn("/api/xo-projects/{project_id}/commits", paths)
        self.assertIn(
            "/api/xo-projects/{project_id}/commits/{sha}/snapshot", paths
        )


if __name__ == "__main__":
    unittest.main()
