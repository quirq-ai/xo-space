"""What a chat's threads made (v2): shares, files a turn wrote, links in
replies and branches become Made items, the person pins them, and what a
thread shared is copied under .xo/made/ before its worktree goes."""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

import httpx

from services.cowork_agent.storm import made
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.made import Made
from services.cowork_agent.storm.worktrees import state, trees
from tests.storm_comms_kit import CHOICE, CommsTestCase
from tests.storm_worktrees_kit import sh
from tests.test_storm_thread_git import commit
from tests.test_storm_worktrees_engine import CHAT, TS, Case, app


class LinkTests(unittest.TestCase):
    def test_markdown_and_bare_links_once_each(self) -> None:
        text = ("Opened [PR #37](https://github.com/o/r/pull/37). See https://example.com/docs, and "
                "https://github.com/o/r/pull/37 again; not ftp://x or example.com.")
        self.assertEqual(made.links(text), [("https://github.com/o/r/pull/37", "PR #37"),
                                            ("https://example.com/docs", "https://example.com/docs")])


class MadeTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.made = Made()
        self.made.start(self.kit.engine)
        self.addAsyncCleanup(self.made.stop)
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        root = await self.owner_message(self.chat, "Write docs")
        started = await self.call(self.pc_token, "create_task", message_id=root["id"], title="Docs",
                                  instructions="x", **CHOICE)
        self.ts, self.sid = root["id"], started["session_id"]
        self.thread_token = await self.token(self.sid)
        self.url = f"/api/chat/chats/{self.chat['id']}/made"
        self.folder = self.kit.projects / "demo"

    async def settled(self) -> None:
        await self.kit.until(lambda: not self.made._tasks)

    async def listed(self, **params) -> list[dict]:
        await self.settled()
        answer = await self.http.get(self.url, params=params)
        self.assertEqual(answer.status_code, 200, answer.text)
        return answer.json()["made"]

    async def turn(self, steps: list[tuple[str, dict]]) -> str:
        """A turn of the thread's session whose stored steps are ``steps``, ended."""
        turn_id = f"turn_{len(self.rows('SELECT id FROM turns'))}"

        def write(w: Writes) -> None:
            w.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES "
                      "(?, ?, 0, 'person', 'running', ?)", (turn_id, self.sid, w.now))
            for type_, data in steps:
                w.append(type_, data, chat_id=self.chat["id"], session_id=self.sid, thread_ts=self.ts,
                         turn_id=turn_id)
        await self.kit.engine.write(write)
        return turn_id

    async def end(self, turn_id: str) -> None:
        def write(w: Writes) -> None:
            w.execute("UPDATE turns SET status = 'finished', ended_at = ? WHERE id = ?", (w.now, turn_id))
            w.append("turn.finished", {}, chat_id=self.chat["id"], session_id=self.sid, thread_ts=self.ts,
                     turn_id=turn_id)
        await self.kit.engine.write(write)

    async def test_a_shared_file_and_an_image_are_made_and_served_by_project_path(self) -> None:
        (self.folder / "notes.md").write_text("# Notes")
        (self.folder / "chart.png").write_bytes(b"\x89PNG")
        await self.call(self.thread_token, "share_files", paths=["notes.md", "chart.png"])
        items = {i["name"]: i for i in await self.listed()}
        self.assertEqual(set(items), {"notes.md", "chart.png"})
        notes, chart = items["notes.md"], items["chart.png"]
        self.assertEqual({k: notes[k] for k in ("kind", "source", "mime", "bytes", "thread_ts", "exists", "where",
                                                "project_id", "relative_path", "pinned", "url")},
                         {"kind": "file", "source": "share", "mime": "text/markdown", "bytes": 7, "thread_ts": self.ts,
                          "exists": True, "where": "made", "project_id": "demo", "relative_path": "notes.md",
                          "pinned": False, "url": None})
        self.assertEqual((chart["kind"], chart["mime"]), ("image", "image/png"))
        (event,) = [json.loads(d) for (d,) in self.rows("SELECT data FROM events WHERE type = 'made'")]
        self.assertEqual({i["name"] for i in event["items"]}, {"notes.md", "chart.png"})
        (self.folder / "notes.md").unlink()
        self.assertFalse({i["name"]: i for i in await self.listed()}["notes.md"]["exists"])

    async def test_a_turns_successful_writes_are_made_and_tied_to_its_reply(self) -> None:
        (self.folder / "app.py").write_text("print(1)\n")
        turn_id = await self.turn([
            ("step.tool", {"call_id": "c1", "tool": "Write", "kind": "create_file", "title": "Write app.py",
                           "target": str(self.folder / "app.py")}),
            ("step.tool_done", {"call_id": "c1", "status": "ok", "size": 9}),
            ("step.tool", {"call_id": "c2", "tool": "Edit", "kind": "edit_file", "title": "Edit x",
                           "target": "x.py"}),
            ("step.tool_done", {"call_id": "c2", "status": "error"}),
            ("step.tool", {"call_id": "c3", "tool": "Write", "kind": "create_file", "title": "Write",
                           "target": "node_modules/pkg/index.js"}),
            ("step.tool_done", {"call_id": "c3", "status": "ok"}),
            ("step.tool", {"call_id": "c4", "tool": "Read", "kind": "read", "title": "Read", "target": "README"}),
            ("step.tool_done", {"call_id": "c4", "status": "ok"}),
        ])
        reply = await self.call(self.thread_token, "reply", text="Wrote [the app](https://example.com/app).")
        await self.end(turn_id)
        items = {i["name"]: i for i in await self.listed()}
        self.assertEqual(set(items), {"app.py", "the app"})
        app_item, link = items["app.py"], items["the app"]
        self.assertEqual((app_item["source"], app_item["turn_id"], app_item["message_id"], app_item["bytes"]),
                         ("write", turn_id, reply["message_id"], 9))
        self.assertEqual((link["kind"], link["url"], link["message_id"], link["turn_id"], link["exists"]),
                         ("link", "https://example.com/app", reply["message_id"], turn_id, True))
        # Shared later: one item, now a share.
        await self.call(self.thread_token, "share_files", paths=["app.py"])
        items = [i for i in await self.listed() if i["name"] == "app.py"]
        self.assertEqual([(i["source"], i["message_id"]) for i in items], [("share", reply["message_id"])])

    async def test_the_person_pins_and_lists_pinned_and_pages(self) -> None:
        for name in ("a.md", "b.md", "c.md"):
            (self.folder / name).write_text(name)
            await self.call(self.thread_token, "share_files", paths=[name])
            self.kit.clock.advance(1)
        listed = await self.listed()
        self.assertEqual([i["name"] for i in listed], ["c.md", "b.md", "a.md"])
        pinned = await self.http.put(f"{self.url}/{listed[2]['id']}/pin")
        self.assertEqual((pinned.status_code, pinned.json()["pinned"]), (200, True))
        self.assertEqual([i["name"] for i in await self.listed(pinned="true")], ["a.md"])
        page = (await self.http.get(self.url, params={"limit": 2})).json()
        self.assertEqual([i["name"] for i in page["made"]], ["c.md", "b.md"])
        rest = (await self.http.get(self.url, params={"before": page["next_before"]})).json()["made"]
        self.assertEqual([i["name"] for i in rest], ["a.md"])
        self.assertEqual([i["name"] for i in await self.listed(since=listed[1]["updated_at"])], ["c.md", "b.md"])
        unpinned = await self.http.delete(f"{self.url}/{listed[2]['id']}/pin")
        self.assertFalse(unpinned.json()["pinned"])
        missing = await self.http.put(f"{self.url}/made_nope/pin")
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "made_not_found"))
        bad = await self.http.get(self.url, params={"since": "soon"})
        self.assertEqual((bad.status_code, bad.json()["detail"]["code"]), (400, "bad_since"))

    async def test_a_thread_branch_with_commits_is_made_once(self) -> None:
        git = {"branch": "xo/thread-abc123", "branch_exists": True, "commits": 2, "merged": False}
        for _ in range(2):
            await self.kit.engine.write(lambda w: w.append("thread.git", {"ts": self.ts, "git": git},
                                                           chat_id=self.chat["id"], thread_ts=self.ts))
        await self.kit.engine.write(lambda w: w.append("thread.git", {"ts": self.ts, "git": {**git, "commits": 0,
                                                                                              "branch": "other"}},
                                                       chat_id=self.chat["id"], thread_ts=self.ts))
        (branch,) = await self.listed()
        self.assertEqual((branch["kind"], branch["name"], branch["path"], branch["exists"]),
                         ("branch", "xo/thread-abc123", None, False))       # no thread_git row: not known to exist
        self.assertEqual(len(self.rows("SELECT 1 FROM events WHERE type = 'made'")), 1)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class CopyOnRemovalTests(Case):
    async def test_what_a_thread_shared_is_copied_before_its_worktree_goes(self) -> None:
        gh = mock.patch.object(state, "gh_available", return_value=False)
        gh.start()
        self.addCleanup(gh.stop)
        self.made = Made()
        await self.boot()
        self.made.start(self.engine)
        self.addAsyncCleanup(self.made.stop)
        self.trees.before_removal.append(self.made.before_removal)
        sid = await self.thread()
        await self.run_turn(sid, 1)
        await self.release(sid)
        tree = trees.tree_path(self.project, TS)
        (tree / "out").mkdir()
        (tree / "out" / "report.md").write_text("# Report")
        (tree / "kept.md").write_text("in git")
        commit(tree, "kept.md")
        sh(self.project, "merge", "-q", "--no-ff", "-m", "Merge", self.row()["branch"])
        await self.engine.write(lambda w: w.append("share", {"files": [
            {"path": "out/report.md", "name": "report.md", "type": "text/markdown", "bytes": 8}],
            "folder": str(tree)}, chat_id=CHAT, thread_ts=TS, session_id=sid))
        await self.engine.write(lambda w: w.execute(       # written, never shared: not copied
            "INSERT INTO made (id, seq, chat_id, thread_ts, kind, source, path, name, created_at, updated_at) "
            "VALUES ('made_kept', 1, ?, ?, 'file', 'write', ?, 'kept.md', ?, ?)",
            (CHAT, TS, str(tree / "kept.md"), w.now, w.now)))
        await self.kit.until(lambda: not self.made._tasks)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://127.0.0.1") as http:
            refused = await http.delete(f"/api/chat/worktrees/{CHAT}/{TS}")
            confirm = refused.json()["detail"]["confirm"]           # out/report.md is untracked
            self.assertFalse((self.project / ".xo" / "made").exists())     # nothing copied for a refusal
            done = await http.delete(f"/api/chat/worktrees/{CHAT}/{TS}", params={"confirm": confirm})
            self.assertEqual(done.status_code, 200, done.text)
            self.assertFalse(tree.exists())
            copy = self.project / ".xo" / "made" / f"thread-{TS}" / "out" / "report.md"
            self.assertEqual(copy.read_text(), "# Report")
            self.assertEqual((self.project / ".xo" / "made" / ".gitignore").read_text().splitlines()[-1], "*")
            self.assertNotIn(".xo/made", self.sh_status())
        items = {i["name"]: i for i in await self.made.listing({"id": CHAT})}      # no chats service here
        report, kept = items["report.md"], items["kept.md"]
        self.assertEqual((report["exists"], report["where"], report["relative_path"]),
                         (True, "copy", f".xo/made/thread-{TS}/out/report.md"))
        self.assertEqual((kept["exists"], kept["where"], kept["relative_path"]), (True, "project", "kept.md"))

    def sh_status(self) -> str:
        return sh(self.project, "status", "--porcelain", "--untracked-files=all")


if __name__ == "__main__":
    unittest.main()
