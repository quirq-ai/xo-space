"""The projects-chat store: channels, timeline and thread messages, threads,
cards, decisions and session records, all under the state root."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.projects_chat import store


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.state = base / "state"
        self.projects = base / "xo-projects"
        (self.projects / "acs" / ".xo").mkdir(parents=True)
        (self.projects / "acs" / ".xo" / "project.json").write_text(
            json.dumps({"pid": "11111111-1111-4111-8111-111111111111", "name": "acs"}))
        env = mock.patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.state), "XO_PROJECTS_ROOT": str(self.projects),
        })
        env.start()
        self.addCleanup(env.stop)


class ChannelTests(_Sandbox):
    def test_the_workspace_and_a_project_have_separate_channels(self) -> None:
        ws = store.ensure_channel(None)
        proj = store.ensure_channel("acs")
        self.assertEqual(ws["id"], "chan_workspace")
        self.assertEqual(proj["id"], "chan_11111111-1111-4111-8111-111111111111")
        self.assertTrue((self.state / "projects" / "workspace.channel" / "channel.json").is_file())
        self.assertTrue((self.state / "projects" / "11111111-1111-4111-8111-111111111111"
                         / "channel" / "channel.json").is_file())
        self.assertEqual(store.ensure_channel("acs"), proj, "idempotent")

    def test_nothing_is_written_into_the_project_folder(self) -> None:
        store.ensure_channel("acs")
        store.post_message("acs", author_kind="owner", author_id="user_1", text="hello")
        self.assertEqual(sorted(p.name for p in (self.projects / "acs").rglob("*")), [".xo", "project.json"])

    def test_an_unknown_project_is_refused(self) -> None:
        with self.assertRaises(store.StoreError) as ctx:
            store.ensure_channel("nope")
        self.assertEqual(ctx.exception.code, "no_such_project")


class MessageAndThreadTests(_Sandbox):
    def test_timeline_rows_follow_the_line_rules_and_sort_by_id(self) -> None:
        a = store.post_message("acs", author_kind="owner", author_id="user_1", text="first")
        b = store.post_message("acs", author_kind="agent", author_id="coordinator", text="second",
                               author_session_id="cse_abcdef0123456789abcdef01")
        rows = store.read_messages("acs")
        self.assertEqual([r["id"] for r in rows], [a["id"], b["id"]])
        self.assertEqual(list(rows[0])[:2], ["ts", "type"])
        self.assertTrue(rows[0]["ts"].endswith("Z"))
        self.assertEqual(rows[0]["pid"], "11111111-1111-4111-8111-111111111111")
        self.assertRegex(a["id"], store.MESSAGE_ID)
        self.assertEqual(store.read_messages("acs", after=a["id"]), [rows[1]])

    def test_a_thread_is_rooted_on_a_timeline_message_once(self) -> None:
        root = store.post_message("acs", author_kind="owner", author_id="user_1", text="build it")
        thread = store.create_thread("acs", root_id=root["id"], title="Build", session_id="cse_x")
        self.assertEqual(thread["ts"], root["id"])
        with self.assertRaises(store.StoreError) as ctx:
            store.create_thread("acs", root_id=root["id"], title="Again", session_id="cse_y")
        self.assertEqual(ctx.exception.code, "already_running")
        reply = store.post_message("acs", author_kind="agent", author_id="thread", text="done",
                                   thread_ts=root["id"])
        self.assertEqual(reply["where"], "thread")
        self.assertEqual(store.read_messages("acs", thread_ts=root["id"]), [reply])
        self.assertEqual(store.get_message("acs", reply["id"]), reply)

    def test_cards_status_and_state(self) -> None:
        root = store.post_message("acs", author_kind="owner", author_id="user_1", text="go")
        store.create_thread("acs", root_id=root["id"], title="Go", session_id="cse_x")
        store.write_status("acs", root["id"], "✱ Building\n○ Test")
        store.write_card("acs", root["id"], {"state": "done", "headline": "Built"})
        store.update_thread("acs", root["id"], state="done")
        [listed] = store.list_threads("acs")
        self.assertEqual(listed["state"], "done")
        self.assertEqual(listed["card"]["headline"], "Built")
        self.assertEqual(store.read_status("acs", root["id"]), "✱ Building\n○ Test")
        with self.assertRaises(store.StoreError):
            store.update_thread("acs", root["id"], state="exploded")

    def test_a_forwarded_copy_keeps_its_origin(self) -> None:
        original = store.post_message(None, author_kind="owner", author_id="user_1", text="fix acs and blog")
        copy = store.post_message("acs", author_kind="owner", author_id="user_1", text=original["text"],
                                  forwarded_from={"channel_id": "chan_workspace", "message_id": original["id"]})
        self.assertNotEqual(copy["id"], original["id"])
        self.assertEqual(copy["forwarded_from"]["message_id"], original["id"])


class SessionTests(_Sandbox):
    def test_a_session_record_lives_in_its_own_folder_and_its_token_resolves(self) -> None:
        record = store.create_session("acs", role="thread", cwd="/w", thread_ts="cmsg_root")
        token = record.pop("token")
        folder = (self.state / "projects" / "11111111-1111-4111-8111-111111111111" / "sessions" / record["id"])
        self.assertTrue((folder / "session.json").is_file())
        self.assertNotIn(token.split(".", 1)[1], (folder / "session.json").read_text())
        project, found = store.resolve_token(token)
        self.assertEqual((project, found["id"]), ("acs", record["id"]))
        self.assertIsNone(store.resolve_token(record["id"] + ".wrong"))
        self.assertEqual([e["type"] for e in store.read_session_events("acs", record["id"])], ["session.created"])

    def test_workspace_sessions_live_in_the_space_sessions_folder(self) -> None:
        record = store.create_session(None, role="workspace-coordinator", cwd="/xo")
        self.assertTrue((self.state / "sessions" / record["id"] / "session.json").is_file())
        self.assertEqual(store.resolve_token(record["token"])[0], None)

    def test_a_retired_session_token_stops_working(self) -> None:
        record = store.create_session("acs", role="project-coordinator", cwd="/w")
        store.update_session("acs", record["id"], retired_at=store.now_ms_iso())
        self.assertIsNone(store.resolve_token(record["token"]))


if __name__ == "__main__":
    unittest.main()
