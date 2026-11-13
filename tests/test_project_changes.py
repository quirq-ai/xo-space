"""What a worktree changed (services/project_files/changes, GET /changes):
git's own patches, against the merge base, without running anything the
repository's config or attributes name and without trusting its index;
plus the routes around saving (the writer key, the Host check)."""

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

from services import git_safe
from services.project_files import changes, writer_key
from services.project_files.address import FileRefused, version_of

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class ChangesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.project = self.base / "projects" / "demo"
        self.project.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.base / "projects"),
                                           "QUIRQ_STATE_ROOT": str(self.base / "state"), **GIT_ENV})
        env.start()
        self.addCleanup(env.stop)
        sh(self.project, "init", "-q", "-b", "main")
        (self.project / ".gitignore").write_text(".xo/\nbuild/\n")
        (self.project / "a.txt").write_text("one\ntwo\nthree\n")
        (self.project / "keep.md").write_text("# keep\n" + "line\n" * 30)
        sh(self.project, "add", ".")
        sh(self.project, "commit", "-q", "-m", "base")
        sh(self.project, "worktree", "add", "-q", ".xo/worktrees/t1", "-b", "xo/t1")
        self.tree = self.project / ".xo/worktrees/t1"

    def objects(self) -> int:
        return sum(len(files) for _, _, files in os.walk(self.project / ".git" / "objects"))

    async def test_a_worktree_s_changes_come_with_git_s_patches(self) -> None:
        (self.tree / "a.txt").write_text("one\nTWO\nthree\n")
        (self.tree / "new.txt").write_text("fresh\n")
        sh(self.tree, "mv", "keep.md", "kept.md")
        (self.tree / "blob.bin").write_bytes(bytes(range(256)))
        (self.tree / "build").mkdir()
        (self.tree / "build" / "out.js").write_text("ignored\n")
        got = await changes.changes("demo", "t1")
        self.assertEqual((got["root"], got["base_source"], got["truncated"], got["total_files"]),
                         (".xo/worktrees/t1", "branch_created", False, 4))
        files = {f["path"]: f for f in got["files"]}
        self.assertEqual(set(files), {"a.txt", "new.txt", "kept.md", "blob.bin"}, "ignored files stay out")
        a = files["a.txt"]
        self.assertEqual((a["status"], a["additions"], a["deletions"]), ("modified", 1, 1))
        self.assertTrue(a["patch"].startswith("diff --git a/a.txt b/a.txt\n"))
        self.assertIn("-two\n+TWO\n", a["patch"])
        self.assertEqual(a["version"], version_of(b"one\nTWO\nthree\n"))
        self.assertEqual((files["kept.md"]["status"], files["kept.md"]["old_path"]), ("renamed", "keep.md"))
        self.assertIn("rename from keep.md", files["kept.md"]["patch"])
        self.assertEqual((files["new.txt"]["status"], files["blob.bin"]["binary"], files["blob.bin"]["patch"],
                          files["blob.bin"]["patch_omitted"]), ("added", True, None, "binary"))

    async def test_it_compares_with_the_merge_base_after_main_moves_on(self) -> None:
        (self.project / "a.txt").write_text("main moved\n")
        sh(self.project, "commit", "-q", "-am", "main moves")
        (self.tree / "new.txt").write_text("x\n")
        sh(self.tree, "add", "new.txt")
        sh(self.tree, "commit", "-q", "-m", "thread commits")
        got = await changes.changes("demo", "t1")
        self.assertEqual([f["path"] for f in got["files"]], ["new.txt"], "main's own change isn't the thread's")
        self.assertNotEqual(got["base_commit"], got["head_commit"])

    async def test_a_thread_is_measured_from_where_it_started_not_from_main(self) -> None:
        # The project works on a line far ahead of main; the thread starts from that line.
        sh(self.project, "checkout", "-q", "-b", "line")
        for n in range(3):
            (self.project / f"line{n}.txt").write_text("x\n")
            sh(self.project, "add", ".")
            sh(self.project, "commit", "-q", "-m", f"line {n}")
        start = sh(self.project, "rev-parse", "HEAD").strip()
        sh(self.project, "worktree", "add", "-q", ".xo/worktrees/t2", "-b", "xo/t2")
        tree = self.project / ".xo/worktrees/t2"
        admin = Path(sh(tree, "rev-parse", "--absolute-git-dir").strip())
        (admin / "XO_THREAD").write_text(f'{{"thread": "t", "chat": "c", "base_commit": "{start}"}}')
        (tree / "mine.txt").write_text("the thread's own\n")
        got = await changes.changes("demo", "t2")
        self.assertEqual((got["base_source"], got["base_commit"]), ("thread_start", start))
        self.assertEqual([f["path"] for f in got["files"]], ["mine.txt"], "the line's history isn't the thread's")
        # Without the marker, the branch's own reflog says where it started.
        (admin / "XO_THREAD").unlink()
        got = await changes.changes("demo", "t2")
        self.assertEqual((got["base_source"], [f["path"] for f in got["files"]]), ("branch_created", ["mine.txt"]))
        # A marker naming a commit the thread doesn't descend from is ignored.
        (admin / "XO_THREAD").write_text('{"base_commit": "' + "0" * 40 + '"}')
        self.assertEqual((await changes.changes("demo", "t2"))["base_source"], "branch_created")

    async def test_a_branch_moved_onto_another_line_is_measured_from_there(self) -> None:
        # Threads start from main, then their agent resets the branch onto the line it works on.
        sh(self.project, "checkout", "-q", "-b", "line")
        for n in range(3):
            (self.project / f"line{n}.txt").write_text("x\n")
            sh(self.project, "add", ".")
            sh(self.project, "commit", "-q", "-m", f"line {n}")
        line = sh(self.project, "rev-parse", "HEAD").strip()
        main = sh(self.project, "rev-parse", "main").strip()
        admin = Path(sh(self.tree, "rev-parse", "--absolute-git-dir").strip())
        (admin / "XO_THREAD").write_text(f'{{"base_commit": "{main}"}}')
        sh(self.tree, "reset", "-q", "--hard", line)
        for n in range(2):
            (self.tree / f"mine{n}.txt").write_text("x\n")
            sh(self.tree, "add", ".")
            sh(self.tree, "commit", "-q", "-m", f"mine {n}")
        got = await changes.changes("demo", "t1")
        self.assertEqual((got["base_source"], got["base_commit"]), ("branch_moved", line))
        self.assertEqual(sorted(f["path"] for f in got["files"]), ["mine0.txt", "mine1.txt"])
        # Undoing its own last commit doesn't move the start.
        sh(self.tree, "reset", "-q", "--hard", "HEAD~1")
        got = await changes.changes("demo", "t1")
        self.assertEqual((got["base_commit"], [f["path"] for f in got["files"]]), (line, ["mine0.txt"]))

    async def test_a_branch_rebased_onto_another_line_is_measured_from_there(self) -> None:
        # The branch's own reflog has only "rebase (finish): ... onto <sha>" (the start entry is HEAD's).
        main = sh(self.project, "rev-parse", "main").strip()
        admin = Path(sh(self.tree, "rev-parse", "--absolute-git-dir").strip())
        (admin / "XO_THREAD").write_text(f'{{"base_commit": "{main}"}}')
        (self.tree / "mine.txt").write_text("x\n")
        sh(self.tree, "add", ".")
        sh(self.tree, "commit", "-q", "-m", "mine")
        sh(self.project, "checkout", "-q", "-b", "line")
        for n in range(3):
            (self.project / f"line{n}.txt").write_text("x\n")
            sh(self.project, "add", ".")
            sh(self.project, "commit", "-q", "-m", f"line {n}")
        line = sh(self.project, "rev-parse", "HEAD").strip()
        sh(self.tree, "rebase", "-q", "line")
        got = await changes.changes("demo", "t1")
        self.assertEqual((got["base_source"], got["base_commit"]), ("branch_moved", line))
        self.assertEqual([f["path"] for f in got["files"]], ["mine.txt"], "the line's commits aren't the thread's")

    async def test_a_large_file_s_patch_is_left_out_before_it_is_made(self) -> None:
        # The size guard reads blob sizes by full id: a file over PATCH_SOURCE_MAX gets no patch,
        # and git is not asked to diff it.
        (self.tree / "big.csv").write_text("x,y\n" * (changes.PATCH_SOURCE_MAX // 4 + 10))
        (self.tree / "a.txt").write_text("one\nTWO\nthree\n")
        diffed: list[str] = []
        real_blocks = changes._blocks

        def blocks(out: str) -> dict:
            diffed.append(out)
            return real_blocks(out)

        with mock.patch.object(changes, "_blocks", blocks):
            got = await changes.changes("demo", "t1")
        files = {f["path"]: f for f in got["files"]}
        self.assertEqual((files["big.csv"]["patch"], files["big.csv"]["patch_omitted"]), (None, "too_large"))
        self.assertIn("+TWO", files["a.txt"]["patch"])
        self.assertTrue(diffed)
        self.assertFalse(any("big.csv" in out for out in diffed), "the large file was diffed")

    async def test_spaces_own_xo_folder_is_not_the_projects_change(self) -> None:
        # A cloned repository gets .xo/ from Space (untracked, not ignored); its stores keep
        # writing a committed one. Neither is the person's change; their own files still are.
        (self.project / ".gitignore").write_text("build/\n")
        xo = self.project / ".xo"
        xo.mkdir(exist_ok=True)
        (xo / "todos.json").write_text("{}\n")
        sh(self.project, "add", ".xo/todos.json", ".gitignore")
        sh(self.project, "commit", "-q", "-m", "commit .xo as XO does")
        (xo / "todos.json").write_text('{"sessions": {}}\n')
        for name in ("project.json", "peers.json", "workitems.json"):
            (xo / name).write_text("{}\n")
        (self.project / "notes.md").write_text("mine\n")
        got = await changes.changes("demo")
        self.assertEqual([f["path"] for f in got["files"]], ["notes.md"])
        self.assertEqual(got["total_files"], 1)
        # In a thread's worktree only an agent writes .xo/: the person sees it.
        (self.tree / ".gitignore").write_text("build/\n")
        (self.tree / ".xo").mkdir()
        (self.tree / ".xo" / "peers.json").write_text('{"peers": ["someone"]}\n')
        paths = [f["path"] for f in (await changes.changes("demo", "t1"))["files"]]
        self.assertIn(".xo/peers.json", paths)

    async def test_a_git_too_old_for_attr_source_is_said_plainly(self) -> None:
        with mock.patch.object(git_safe, "version", mock.AsyncMock(return_value=(2, 39))):
            with self.assertRaises(FileRefused) as caught:
                await changes.changes("demo", "t1")
        self.assertEqual((caught.exception.code, caught.exception.status), ("git_too_old", 501))
        self.assertIn("2.40", caught.exception.message)

    async def test_git_s_version_is_read_from_apple_s_banner_too(self) -> None:
        for banner, expected in (("git version 2.39.5 (Apple Git-154)", (2, 39)), ("git version 2.56.0", (2, 56)),
                                 ("git version 2.45.2.windows.1", (2, 45)), ("nonsense", (0, 0))):
            with self.subTest(banner=banner), mock.patch.object(git_safe, "_version", None), \
                    mock.patch.object(git_safe, "git_out", mock.AsyncMock(return_value=banner)):
                self.assertEqual(await git_safe.version(), expected)

    async def test_a_file_named_with_quotes_shows_its_own_content(self) -> None:
        # hash-object --stdin-paths would unquote '"q.txt"' and hash q.txt instead.
        (self.tree / "q.txt").write_text("decoy\n")
        (self.tree / '"q.txt"').write_text("real\n")
        got = await changes.changes("demo", "t1")
        files = {f["path"]: f for f in got["files"]}
        self.assertIn("+real", files['"q.txt"']["patch"])
        self.assertNotIn("decoy", files['"q.txt"']["patch"])

    async def test_a_fast_forward_onto_another_line_moves_the_start(self) -> None:
        sh(self.project, "checkout", "-q", "-b", "line")
        (self.project / "line.txt").write_text("x\n")
        sh(self.project, "add", ".")
        sh(self.project, "commit", "-q", "-m", "line")
        line = sh(self.project, "rev-parse", "HEAD").strip()
        sh(self.tree, "merge", "-q", "--ff-only", "line")
        (self.tree / "mine.txt").write_text("x\n")
        got = await changes.changes("demo", "t1")
        self.assertEqual((got["base_source"], got["base_commit"], [f["path"] for f in got["files"]]),
                         ("branch_moved", line, ["mine.txt"]))

    async def test_main_merged_in_since_isn_t_the_thread_s_work(self) -> None:
        start = sh(self.project, "rev-parse", "HEAD").strip()
        admin = Path(sh(self.tree, "rev-parse", "--absolute-git-dir").strip())
        (admin / "XO_THREAD").write_text(f'{{"base_commit": "{start}"}}')
        (self.project / "from-main.txt").write_text("main\n")
        sh(self.project, "add", ".")
        sh(self.project, "commit", "-q", "-m", "main moves")
        (self.tree / "mine.txt").write_text("x\n")
        sh(self.tree, "add", "mine.txt")
        sh(self.tree, "commit", "-q", "-m", "mine")
        sh(self.tree, "merge", "-q", "--no-edit", "main")
        got = await changes.changes("demo", "t1")
        self.assertEqual((got["base_source"], got["base_ref"]), ("merge_base", "main"))
        self.assertEqual([f["path"] for f in got["files"]], ["mine.txt"])

    async def test_nothing_the_repository_names_runs_and_its_index_hides_nothing(self) -> None:
        marker = self.base / "PWNED"
        sh(self.project, "config", "filter.evil.clean", f"touch {marker}; cat")
        sh(self.project, "config", "diff.evil.textconv", f"touch {marker}; cat")
        sh(self.project, "config", "diff.external", f"touch {marker}")
        (self.tree / ".gitattributes").write_text("* filter=evil diff=evil\n")
        (self.tree / "a.txt").write_text("changed\n")
        sh(self.tree, "update-index", "--assume-unchanged", "a.txt")
        index = Path(sh(self.tree, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        index_before, objects_before = index.read_bytes(), self.objects()
        marker.unlink(missing_ok=True)          # the test's own git calls above may run it; XO's must not
        got = await changes.changes("demo", "t1")
        self.assertFalse(marker.exists(), "a filter, textconv or external diff ran")
        self.assertIn("a.txt", [f["path"] for f in got["files"]], "assume-unchanged hid an edit")
        self.assertEqual(index.read_bytes(), index_before, "the worktree's own index was written")
        self.assertEqual(self.objects(), objects_before, "objects were written into the repository")

    async def test_info_attributes_select_no_filter_either(self) -> None:
        marker = self.base / "PWNED"
        sh(self.project, "config", "filter.evil.clean", f"sh -c 'touch {marker}; cat'")
        info = self.project / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "attributes").write_text("* filter=evil diff=evil\n")
        (self.tree / "a.txt").write_text("changed\n")
        (self.tree / "new.txt").write_text("new\n")
        marker.unlink(missing_ok=True)
        got = await changes.changes("demo", "t1")
        self.assertFalse(marker.exists(), "a filter selected by .git/info/attributes ran")
        self.assertEqual(sorted(f["path"] for f in got["files"]), ["a.txt", "new.txt"])
        self.assertIn("+changed", got["files"][0]["patch"])

    async def test_a_type_change_or_odd_bytes_never_cost_the_other_patches(self) -> None:
        (self.tree / "a.txt").unlink()
        (self.tree / "a.txt").symlink_to("keep.md")
        (self.tree / "keep.md").write_bytes(b"# keep\nform\x0cfeed and\rcarriage\x1cmid line\n" + b"line\n" * 30)
        got = {f["path"]: f for f in (await changes.changes("demo", "t1"))["files"]}
        self.assertEqual((got["a.txt"]["status"], got["a.txt"]["patch_omitted"]), ("type_changed", "type_changed"))
        patch = got["keep.md"]["patch"]
        self.assertTrue(patch.startswith("diff --git a/keep.md b/keep.md\n"))
        self.assertIn("\rcarriage\x1cmid line\n", patch)

    async def test_gitmodules_can_t_hide_a_submodule_bump(self) -> None:
        sub = self.base / "sub"
        sub.mkdir()
        sh(sub, "init", "-q", "-b", "main")
        (sub / "s.txt").write_text("1\n")
        sh(sub, "add", ".")
        sh(sub, "commit", "-q", "-m", "s1")
        sh(self.tree, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "lib")
        sh(self.tree, "config", "-f", ".gitmodules", "submodule.lib.ignore", "all")
        sh(self.tree, "add", ".gitmodules")
        sh(self.tree, "commit", "-q", "-m", "add lib")
        (self.tree / "lib" / "s.txt").write_text("2\n")
        sh(self.tree / "lib", "commit", "-q", "-am", "s2")
        got = {f["path"]: f for f in (await changes.changes("demo", "t1"))["files"]}
        self.assertEqual(got["lib"]["patch_omitted"], "submodule")

    async def test_the_project_s_own_checkout_and_its_worktrees_stay_apart(self) -> None:
        (self.project / "a.txt").write_text("uncommitted\n")
        (self.tree / "only-in-thread.txt").write_text("x\n")
        got = await changes.changes("demo")
        self.assertEqual((got["root"], got["base_ref"], [f["path"] for f in got["files"]]), ("", "HEAD", ["a.txt"]))

    async def test_one_file_and_the_bounds(self) -> None:
        (self.tree / "a.txt").write_text("one\nTWO\nthree\n")
        (self.tree / "new.txt").write_text("fresh\n")
        got = await changes.changes("demo", "t1", path="new.txt")
        self.assertEqual([f["path"] for f in got["files"]], ["new.txt"])
        with mock.patch.object(changes, "PATCH_BUDGET", 10):
            got = await changes.changes("demo", "t1")
        self.assertEqual({f["patch_omitted"] for f in got["files"]}, {"response_budget"})
        with mock.patch.object(changes, "FILES_MAX", 1):
            got = await changes.changes("demo", "t1")
        self.assertEqual((got["truncated"], got["total_files"], len(got["files"])), (True, 2, 1))

    async def test_only_this_project_s_worktrees(self) -> None:
        (self.project / ".xo/worktrees/fake").mkdir(parents=True)
        for name in ("fake", "missing", "../..", "a/b"):
            with self.subTest(name=name):
                with self.assertRaises(FileRefused) as caught:
                    await changes.changes("demo", name)
                self.assertEqual(caught.exception.code, "worktree_not_found")


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        from routers.cowork_agent.bff.xo_project_files import router as files_router
        from routers.cowork_agent.bff.xo_projects import router as projects_router

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.project = self.base / "projects" / "demo"
        self.project.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.base / "projects"),
                                           "QUIRQ_STATE_ROOT": str(self.base / "state")})
        env.start()
        self.addCleanup(env.stop)
        app = FastAPI()
        app.include_router(projects_router)
        app.include_router(files_router)
        self.client = TestClient(app, base_url="http://127.0.0.1:5002")
        self.key = (writer_key.ensure(), writer_key.current())[1]
        (self.project / "a.md").write_text("one\n")

    def put(self, body: dict, key: str | None = "default"):
        headers = {} if key is None else {"X-XO-UI-Writer": self.key if key == "default" else key}
        return self.client.put("/api/xo-projects/demo/file", json=body, headers=headers)

    def test_file_says_its_version_and_a_save_round_trips(self) -> None:
        got = self.client.get("/api/xo-projects/demo/file", params={"relative_path": "a.md"}).json()
        self.assertEqual((got["version"], got["editable"], got["place"]), (version_of(b"one\n"), True, "ordinary"))
        res = self.put({"relative_path": "a.md", "content": "two\n", "base_version": got["version"]})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["version"], version_of(b"two\n"))
        stale = self.put({"relative_path": "a.md", "content": "three\n", "base_version": got["version"]})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["current"]["content"], "two\n")
        undo = self.client.post(f"/api/xo-projects/demo/edits/{res.json()['edit_id']}/undo",
                                headers={"X-XO-UI-Writer": self.key})
        self.assertEqual(undo.status_code, 200, undo.text)
        self.assertEqual((self.project / "a.md").read_text(), "one\n")
        listed = self.client.get("/api/xo-projects/demo/edits").json()
        self.assertEqual([i["kind"] for i in listed["items"]], ["undo", "save"])

    def test_saves_need_the_ui_writer_key(self) -> None:
        for key in (None, "", "0" * 64):
            with self.subTest(key=key):
                res = self.put({"relative_path": "a.md", "content": "x", "base_version": version_of(b"one\n")},
                               key=key)
                self.assertEqual((res.status_code, res.json()["detail"]["code"]), (403, "ui_only"))
        self.assertEqual((self.project / "a.md").read_text(), "one\n")
        res = self.client.post("/api/xo-projects/demo/edits/e_x/undo")
        self.assertEqual(res.status_code, 403)

    def test_a_body_must_say_where_it_started(self) -> None:
        res = self.put({"relative_path": "a.md", "content": "x"})
        self.assertEqual(res.status_code, 422, "base_version is required (null to create)")
        res = self.put({"relative_path": "a.md", "content": "x", "base_version": None, "extra": 1})
        self.assertEqual(res.status_code, 422)

    def test_files_answer_only_hosts_rebinding_cant_reach(self) -> None:
        for base in ("http://evil.example:5002", "http://evil.example:8080"):
            with self.subTest(base=base):
                client = TestClient(self.client.app, base_url=base)
                res = client.get("/api/xo-projects/demo/file", params={"relative_path": "a.md"})
                self.assertEqual((res.status_code, res.json()["detail"]["code"]), (403, "host_not_allowed"))
        for base in ("http://localhost:5002", "http://[::1]:5002", "http://app.localhost:5002"):
            with self.subTest(base=base):
                client = TestClient(self.client.app, base_url=base)
                self.assertEqual(client.get("/api/xo-projects/demo/file",
                                            params={"relative_path": "a.md"}).status_code, 200)

    def test_tree_and_download_show_hidden_files_when_asked(self) -> None:
        (self.project / ".env").write_text("A=1\n")
        (self.project / "x.swp").write_text("tmp")
        plain = self.client.get("/api/xo-projects/demo/tree").json()
        self.assertEqual([f["name"] for f in plain["files"]], ["a.md"])
        shown = self.client.get("/api/xo-projects/demo/tree", params={"hidden": "true"}).json()
        self.assertEqual([f["name"] for f in shown["files"]], [".env", "a.md"])
        self.assertEqual(self.client.get("/api/xo-projects/demo/download",
                                         params={"relative_path": ".env"}).status_code, 404)
        self.assertEqual(self.client.get("/api/xo-projects/demo/download",
                                         params={"relative_path": ".env", "hidden": "true"}).content, b"A=1\n")


class ReadHostTests(unittest.TestCase):
    def allowed(self, host: str, *, server_port: int = 5002, trusted: tuple[str, ...] = ()) -> bool:
        from starlette.requests import Request

        from routers.browser_guard import TRUSTED_ORIGINS_KEY, read_host_allowed

        scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"host", host.encode())],
                 "scheme": "http", "server": ("127.0.0.1", server_port), "query_string": b"",
                 TRUSTED_ORIGINS_KEY: frozenset(trusted)}
        return read_host_allowed(Request(scope))

    def test_the_hosts(self) -> None:
        coder = "https://{{port}}--main--ws--me.coder.example.com"
        with mock.patch.dict(os.environ, {"VSCODE_PROXY_URI": coder}):
            self.assertTrue(self.allowed("127.0.0.1:5002"))
            self.assertTrue(self.allowed("localhost:3400"))
            self.assertTrue(self.allowed("5002--main--ws--me.coder.example.com"), "this workspace's Coder app")
            self.assertFalse(self.allowed("5002--main--ws--me.coder.example.com:8443"))
            self.assertFalse(self.allowed("evil.5002--main--ws--me.coder.example.com"), "one label only")
            self.assertFalse(self.allowed("other--main--ws--you.coder.example.com"))
            self.assertFalse(self.allowed("attacker.example:5002"))
            self.assertFalse(self.allowed("attacker.example"), "a name without a port is no longer enough")
            self.assertTrue(self.allowed("box.lan:5002", trusted=("http://box.lan:5002",)))
        with mock.patch.dict(os.environ, {"VSCODE_PROXY_URI": ""}):
            self.assertFalse(self.allowed("5002--main--ws--me.coder.example.com"), "outside Coder: not listed")


if __name__ == "__main__":
    unittest.main()
