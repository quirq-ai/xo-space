"""``services/work/runner.py``: one session per Inbox item, over a fake
dispatcher stream. The stream seam (``runner._open_stream``) is patched,
so no runtime is spawned; everything else (the project scaffold, the item
files, the index, the timeline lines) is real, in a temp state root."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.connections import store as connections_store
from services.cowork_agent import project_layout
from services.cowork_agent.project_sharing import status as sharing_status
from services.work import inbox, items, runner, store

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "quirq-state"
PROJECT = ROOT / "tests" / "fixtures" / "xo-project"
NOW = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)
OUTCOME = ('Read it. Drafted a reply in reply.md.\n\n```json\n{"kind": "reply_drafted", "summary": "Sagar asks about the June invoice; '
           'a reply is drafted.", "draft": "reply.md", "task": null, "question": null, "acted": []}\n```\n')


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeDispatcher:
    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name


def stream_of(text: str, *, error: str | None = None, hang: bool = False, native: str = "native-1"):
    """A factory for ``_open_stream``: yields the text as tokens, then done."""
    calls: list[dict] = []

    def open_stream(dispatcher, prompt, **kwargs):
        calls.append({"prompt": prompt, "runtime": dispatcher.agent_name, **kwargs})

        async def gen():
            if hang:
                await asyncio.sleep(3600)
            for word in text.split(" "):
                yield {"type": "token", "token": word + " "}
            if error:
                yield {"type": "error", "error": error}
            yield {"done": True, "native_session_id": native}
        return gen()
    open_stream.calls = calls
    return open_stream


class _Sample(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.state = base / ".quirq"
        self.projects = base / "projects"
        shutil.copytree(FIXTURE, self.state, ignore=shutil.ignore_patterns("README.md"))
        shutil.copytree(PROJECT, self.projects / "sample-project")
        self._env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.projects), "QUIRQ_STATE_ROOT": str(self.state),
                                            "XO_SCHEDULER_ENABLED": "false", "QUIRQ_COMMAND_LOG": "off", "XO_INBOX_SESSIONS": "on"})
        self._env.start()
        sharing_status.reset()
        runner.reset_for_tests()
        self._patches = [
            patch("services.cowork_agent.engine.dispatcher.AgentDispatcher", _FakeDispatcher),
            patch.object(runner, "resolve_agent_name", return_value="sample_agent"),
            patch.object(runner.connections_poller, "resolve_user_id", AsyncMock(return_value=None)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        runner.reset_for_tests()
        sharing_status.reset()
        self._env.stop()
        self._tmp.cleanup()

    def make_item(self, key: str = "m1", *, minutes_ago: int = 5, title: str = "Invoice question") -> str:
        connections_store.append_events("gmail", [{"ts": iso(NOW - timedelta(minutes=minutes_ago)), "type": "unread", "key": key,
                                                   "title": title, "body": "Hi, the June invoice shows 12 seats.",
                                                   "url": "https://mail.google.com/mail/u/0/#inbox/" + key, "toolkit": "gmail"}])
        [item_id] = [i for i in items.make_items("gmail", now=NOW) if i.endswith(key)]
        return item_id

    async def finish(self, toolkit: str = "gmail", item_id: str = "unread-m1") -> None:
        task = runner._running.get((toolkit, item_id))
        if task is not None:
            await task

    def space_types(self) -> list[tuple[str, str | None]]:
        path = self.state / "projects" / "timeline.jsonl"
        return [(json.loads(l)["type"], json.loads(l).get("status")) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


class ParseTests(unittest.TestCase):
    def test_the_last_json_block_is_the_outcome(self) -> None:
        out = runner.parse_outcome("first\n```json\n{\"kind\": \"fyi\", \"summary\": \"old\"}\n```\nthen\n" + OUTCOME)
        self.assertEqual((out["kind"], out["draft"], out["task"], out["acted"]), ("reply_drafted", "reply.md", None, []))
        self.assertTrue(out["summary"].startswith("Sagar asks"))
        task = runner.parse_outcome('```json\n{"kind": "task_proposed", "summary": "s", "task": {"title": " Reissue ", "assignee": "@me"}}\n```')
        self.assertEqual(task["task"], {"title": "Reissue", "assignee": "@me"})
        for text, why in (("no block", "without an outcome block"), ("```json\n{oops}\n```", "not valid JSON"),
                          ('```json\n{"kind": "shrug", "summary": "s"}\n```', "kind"), ('```json\n{"kind": "fyi"}\n```', "summary"),
                          ('```json\n{"kind": "fyi", "summary": "s", "draft": "../x"}\n```', "draft"),
                          ('```json\n{"kind": "fyi", "summary": "s", "task": {}}\n```', "task"),
                          ('```json\n{"kind": "fyi", "summary": "s", "acted": "sent"}\n```', "acted")):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as ctx:
                    runner.parse_outcome(text)
                self.assertIn(why, str(ctx.exception))


class StartTests(_Sample):
    async def test_a_manual_start_runs_the_session_and_records_the_outcome(self) -> None:
        item_id = self.make_item()
        open_stream = stream_of(OUTCOME)
        with patch.object(runner, "_open_stream", open_stream):
            started = await runner.start_item("gmail", item_id)
            self.assertIn(("gmail", item_id), runner.running())
            await self.finish()
        self.assertEqual(started["project_id"], "inbox-gmail")
        self.assertIsNotNone(project_layout.load_project("inbox-gmail"), "the connection project was scaffolded")
        self.assertTrue((self.projects / "inbox-gmail" / "AGENTS.md").is_file())
        self.assertTrue(items.workbench_dir("gmail", item_id).is_dir())
        [call] = open_stream.calls
        self.assertEqual((call["agent_id"], call["agent_type"], call["runtime"], call["is_new"], call["session_id"]),
                         ("inbox-gmail", "inbox-item", "sample_agent", True, started["session_id"]))
        self.assertIn("Acting on the connection: no.", call["prompt"])
        self.assertIn("the June invoice shows 12 seats", call["prompt"])
        self.assertIn(str(items.workbench_dir("gmail", item_id)), call["prompt"])
        record = items.read_item("gmail", item_id)
        self.assertEqual((record["status"], record["outcome"], record["session_id"]), ("done", "reply_drafted", started["session_id"]))
        session = items.read_session("gmail", item_id)
        self.assertEqual((session["exit"]["status"], session["native_session_id"], session["attempt"], session["manual"]), ("ok", "native-1", 1, True))
        self.assertIsNotNone(session["ended_at"])
        self.assertEqual(items.read_outcome("gmail", item_id)["draft"], "reply.md")
        self.assertIn("Drafted a reply", items.log_path("gmail", item_id).read_text(encoding="utf-8"))
        self.assertEqual(runner.running(), [])
        types = [t for t in self.space_types() if t[0].startswith("inbox.item.")]
        self.assertEqual(types, [("inbox.item.created", "new"), ("inbox.item.started", "running"), ("inbox.item.finished", "reply_drafted")])
        project_lines = (self.state / "projects" / project_layout.load_project("inbox-gmail")["pid"] / "timeline.jsonl").read_text(encoding="utf-8")
        self.assertIn("inbox.item.finished", project_lines, "the connection project's own timeline carries it too")
        # the Inbox now shows the draft as a decision
        page = inbox.compose(store.load_document()[0], me=["local"], now=NOW)
        [row] = [d for d in page["decisions"] if d["key"] == items.item_key("gmail", item_id)]
        self.assertEqual((row["reason"], row["primary"], row["ref"]["can_send"], row["ref"]["native_session_id"]), ("item_draft", "open_workbench", False, "native-1"))

    async def test_one_session_per_item_and_the_policy_gates(self) -> None:
        item_id = self.make_item()
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            await runner.start_item("gmail", item_id)
            with self.assertRaises(store.WorkError) as ctx:
                await runner.start_item("gmail", item_id)
            self.assertEqual(ctx.exception.code, "session_exists")
            await self.finish()
            with self.assertRaises(store.WorkError) as ctx:
                await runner.start_item("gmail", item_id)
            self.assertEqual(ctx.exception.code, "session_exists", "done is not failed; no retry")
            with self.assertRaises(store.WorkError) as ctx:
                await runner.start_item("gmail", item_id, retry=True)
            self.assertEqual(ctx.exception.code, "not_failed")
        items.write_policy("gmail", {"items": {"unread": True}, "sessions": {"mode": "off"}})
        other = self.make_item("m2", minutes_ago=4)
        with self.assertRaises(store.WorkError) as ctx:
            await runner.start_item("gmail", other)
        self.assertEqual(ctx.exception.code, "sessions_off")
        with self.assertRaises(store.WorkError) as ctx:
            await runner.start_item("slack", "mention-x")
        self.assertEqual(ctx.exception.status, 404)
        items.mark_decided("gmail", other, "dismissed")
        items.write_policy("gmail", {"items": {"unread": True}})
        with self.assertRaises(store.WorkError) as ctx:
            await runner.start_item("gmail", other)
        self.assertEqual(ctx.exception.code, "item_decided")

    async def test_no_outcome_block_is_a_failure_and_retry_runs_it_again(self) -> None:
        item_id = self.make_item()
        with patch.object(runner, "_open_stream", stream_of("I looked at it and did nothing else.")):
            await runner.start_item("gmail", item_id)
            await self.finish()
        record = items.read_item("gmail", item_id)
        self.assertEqual(record["status"], "failed")
        session = items.read_session("gmail", item_id)
        self.assertEqual(session["exit"]["status"], "error")
        self.assertIn("without an outcome block", session["exit"]["message"])
        self.assertEqual([t for t in self.space_types() if t[0] == "inbox.item.failed"], [("inbox.item.failed", "error")])
        page = inbox.compose(store.load_document()[0], me=["local"], now=NOW)
        [row] = [d for d in page["decisions"] if d["key"] == items.item_key("gmail", item_id)]
        self.assertEqual((row["reason"], row["primary"], row["tone"]), ("item_failed", "retry", "error"))
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            again = await runner.start_item("gmail", item_id, retry=True)
            await self.finish()
        self.assertEqual(again["attempt"], 2)
        self.assertEqual(items.read_item("gmail", item_id)["status"], "done")

    async def test_a_stream_error_and_a_timeout_fail_the_item(self) -> None:
        item_id = self.make_item()
        with patch.object(runner, "_open_stream", stream_of(OUTCOME, error="claude exited 1")):
            await runner.start_item("gmail", item_id)
            await self.finish()
        self.assertEqual(items.read_session("gmail", item_id)["exit"], {"status": "error", "message": "claude exited 1"})
        other = self.make_item("m2", minutes_ago=4)
        quick = items.read_policy("gmail")
        quick["sessions"]["timeout_s"] = 0.05
        with patch.object(runner, "_open_stream", stream_of(OUTCOME, hang=True)), patch.object(runner.items, "read_policy", return_value=quick):
            await runner.start_item("gmail", other)
            await self.finish("gmail", other)
        session = items.read_session("gmail", other)
        self.assertEqual(session["exit"]["status"], "timeout")
        self.assertEqual(items.read_item("gmail", other)["status"], "failed")

    async def test_caps_hold_per_connection(self) -> None:
        items.write_policy("gmail", {"items": {"unread": True}, "sessions": {"max_concurrent": 1, "max_per_hour": 2}})
        a, b, c = self.make_item("a"), self.make_item("b", minutes_ago=4), self.make_item("c", minutes_ago=3)
        with patch.object(runner, "_open_stream", stream_of(OUTCOME, hang=True)):
            await runner.start_item("gmail", a)
            with self.assertRaises(store.WorkError) as ctx:
                await runner.start_item("gmail", b)
            self.assertEqual(ctx.exception.code, "cap_reached")
            await asyncio.sleep(0.02)   # let the session reach its stream before the server "stops"
            task = runner._running[("gmail", a)]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertEqual(items.read_session("gmail", a)["exit"]["status"], "cancelled")
        self.assertEqual(runner.running(), [])
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            await runner.start_item("gmail", b)
            await self.finish("gmail", b)
            with self.assertRaises(store.WorkError) as ctx:
                await runner.start_item("gmail", c)
            self.assertEqual(ctx.exception.code, "cap_reached", "two starts this hour")


class ThreadTests(_Sample):
    async def test_the_first_run_writes_the_agent_turn_without_its_outcome_block(self) -> None:
        item_id = self.make_item()
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            await runner.start_item("gmail", item_id)
            await self.finish()
        [turn] = items.read_thread("gmail", item_id)
        self.assertEqual((turn["type"], turn["outcome"], turn["attempt"]), ("agent", "reply_drafted", 1))
        self.assertEqual(turn["text"], "Read it. Drafted a reply in reply.md.")
        self.assertNotIn("```", turn["text"])

    async def test_a_reply_on_a_new_item_starts_the_session_with_the_note(self) -> None:
        item_id = self.make_item()
        open_stream = stream_of(OUTCOME)
        with patch.object(runner, "_open_stream", open_stream):
            started = await runner.reply_item("gmail", item_id, "Draft it in Hindi, please.")
            await self.finish()
        [call] = open_stream.calls
        self.assertTrue(call["is_new"])
        self.assertIn("The person adds, before you start:\nDraft it in Hindi, please.", call["prompt"])
        thread = items.read_thread("gmail", item_id)
        self.assertEqual([(t["type"], t["text"][:12]) for t in thread], [("person", "Draft it in "), ("agent", "Read it. Dra")])
        self.assertEqual(thread[0]["session_id"], started["session_id"])
        self.assertEqual(items.read_item("gmail", item_id)["status"], "done")

    async def test_a_reply_resumes_the_session_and_needs_no_outcome_block(self) -> None:
        item_id = self.make_item()
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            first = await runner.start_item("gmail", item_id)
            await self.finish()
        follow = stream_of("Shortened it: reply.md is two lines now.", native="native-1")
        with patch.object(runner, "_open_stream", follow):
            again = await runner.reply_item("gmail", item_id, "Keep it to two lines.")
            self.assertEqual(again["session_id"], first["session_id"], "the same session")
            with self.assertRaises(store.WorkError) as ctx:
                await runner.reply_item("gmail", item_id, "and sign it")
            self.assertEqual(ctx.exception.code, "session_running")
            await self.finish()
        [call] = follow.calls
        self.assertFalse(call["is_new"])
        self.assertEqual(call["session_id"], first["session_id"])
        self.assertTrue(call["prompt"].startswith("Keep it to two lines."))
        self.assertIn("otherwise just answer", call["prompt"])
        thread = items.read_thread("gmail", item_id)
        self.assertEqual([t["type"] for t in thread], ["agent", "person", "agent"])
        self.assertEqual(thread[-1]["text"], "Shortened it: reply.md is two lines now.")
        self.assertIsNone(thread[-1].get("outcome"))
        record = items.read_item("gmail", item_id)
        self.assertEqual((record["status"], record["outcome"]), ("done", "reply_drafted"), "the outcome stands")
        self.assertEqual(items.read_outcome("gmail", item_id)["draft"], "reply.md")
        session = items.read_session("gmail", item_id)
        self.assertEqual(session["exit"]["status"], "ok")
        self.assertIsNotNone(session.get("resumed_at"))
        finished = [t for t in self.space_types() if t[0] == "inbox.item.finished"]
        self.assertEqual(finished, [("inbox.item.finished", "reply_drafted"), ("inbox.item.finished", "replied")])

    async def test_a_follow_up_that_restates_the_outcome_changes_it_and_a_failed_one_leaves_the_item_done(self) -> None:
        item_id = self.make_item()
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            await runner.start_item("gmail", item_id)
            await self.finish()
        changed = '```json\n{"kind": "task_proposed", "summary": "Finance must reissue it.", "task": {"title": "Reissue the invoice", "assignee": null}}\n```'
        with patch.object(runner, "_open_stream", stream_of("Agreed, this is a task for finance.\n" + changed)):
            await runner.reply_item("gmail", item_id, "This needs finance, not a reply.")
            await self.finish()
        self.assertEqual(items.read_item("gmail", item_id)["outcome"], "task_proposed")
        self.assertEqual(items.read_thread("gmail", item_id)[-1]["text"], "Agreed, this is a task for finance.")
        with patch.object(runner, "_open_stream", stream_of("oops", error="claude exited 1")):
            await runner.reply_item("gmail", item_id, "Who at finance?")
            await self.finish()
        record = items.read_item("gmail", item_id)
        self.assertEqual((record["status"], record["outcome"]), ("done", "task_proposed"), "a failed follow-up does not fail the item")
        self.assertEqual(items.read_thread("gmail", item_id)[-1]["type"], "system")
        with self.assertRaises(store.WorkError):
            await runner.reply_item("gmail", item_id, "")
        with self.assertRaises(store.WorkError):
            await runner.reply_item("gmail", item_id, "x" * 5000)

    async def test_the_service_thread_answers_running_and_the_reply_flags(self) -> None:
        from services.work import service
        item_id = self.make_item()
        with patch.object(runner, "_open_stream", stream_of(OUTCOME, hang=True)):
            await runner.start_item("gmail", item_id)
            thread = service.item_thread("gmail", item_id)
            self.assertTrue(thread["running"])
            self.assertTrue(thread["can_reply"])
            self.assertFalse(thread["can_send"])
            runner._running[("gmail", item_id)].cancel()
            try:
                await runner._running[("gmail", item_id)]
            except asyncio.CancelledError:
                pass
        thread = service.item_thread("gmail", item_id)
        self.assertFalse(thread["running"])
        self.assertEqual(thread["item"]["status"], "failed")
        self.assertEqual(thread["thread"][-1]["type"], "system")


class LoopTests(_Sample):
    async def test_auto_mode_starts_listed_kinds_and_the_tick_harvests_orphans(self) -> None:
        items.write_policy("gmail", {"items": {"unread": True, "drafts": True}, "sessions": {"mode": "auto", "kinds": ["unread"]}})
        connections_store.append_events("gmail", [
            {"ts": iso(NOW - timedelta(minutes=5)), "type": "unread", "key": "m1", "title": "Mail", "body": "", "url": None, "toolkit": "gmail"},
            {"ts": iso(NOW - timedelta(minutes=4)), "type": "drafts", "key": "d1", "title": "Draft", "body": "", "url": None, "toolkit": "gmail"},
        ])
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            summary = await runner.tick()
            await self.finish("gmail", "unread-m1")
        self.assertEqual((summary["connections"], summary["made"], summary["started"]), (1, 2, 1))
        self.assertEqual(items.read_item("gmail", "unread-m1")["status"], "done")
        self.assertEqual(items.read_item("gmail", "drafts-d1")["status"], "new", "an item of an unlisted kind waits for a person")
        # a session the index says is running with no task behind it is a restart: it fails, and Retry brings it back
        items.write_session("gmail", "drafts-d1", {"session_id": "s-orphan", "native_session_id": None, "runtime": "sample_agent",
                                                    "project_id": "inbox-gmail", "workbench": "items/drafts-d1", "agent_type": "inbox-item",
                                                    "started_at": iso(NOW), "ended_at": None, "exit": None, "attempt": 1, "manual": True})
        items.update_item("gmail", "drafts-d1", status="running", session_id="s-orphan")
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            summary = await runner.tick()
        self.assertEqual(summary["orphaned"], 1)
        self.assertEqual(items.read_session("gmail", "drafts-d1")["exit"]["status"], "orphaned")
        self.assertEqual(items.read_item("gmail", "drafts-d1")["status"], "failed")

    async def test_the_poll_listener_makes_items_at_once(self) -> None:
        connections_store.append_events("gmail", [{"ts": iso(NOW - timedelta(minutes=2)), "type": "unread", "key": "m9",
                                                   "title": "Mail", "body": "", "url": None, "toolkit": "gmail"}])
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            await runner._after_poll("gmail")
            await runner._after_poll("slack")
        self.assertEqual(items.read_item("gmail", "unread-m9")["status"], "new", "manual mode: made, not started")

    async def test_send_needs_act_and_a_draft(self) -> None:
        item_id = self.make_item()
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            await runner.start_item("gmail", item_id)
            await self.finish()
        with self.assertRaises(store.WorkError) as ctx:
            await runner.send_item("gmail", item_id)
        self.assertEqual(ctx.exception.code, "act_not_allowed")
        items.write_policy("gmail", {"items": {"unread": True}, "sessions": {"act": True}})
        sent = '```json\n{"kind": "handled", "summary": "Sent the reply.", "acted": ["replied to Sagar"]}\n```'
        open_stream = stream_of(sent)
        with patch.object(runner, "_open_stream", open_stream):
            result = await runner.send_item("gmail", item_id)
            await self.finish()
        [call] = open_stream.calls
        self.assertEqual((call["is_new"], call["session_id"]), (False, result["session_id"]), "the same session, resumed")
        self.assertIn("approved your draft at reply.md", call["prompt"])
        self.assertEqual(items.read_outcome("gmail", item_id)["acted"], ["replied to Sagar"])
        self.assertEqual(items.read_item("gmail", item_id)["outcome"], "handled")
        page = inbox.compose(store.load_document()[0], me=["local"], now=NOW)
        [row] = [c for c in page["completed"] if c["key"] == items.item_key("gmail", item_id)]
        self.assertEqual((row["kind"], row["detail"]), ("item", "Sent the reply. · replied to Sagar"))
        with self.assertRaises(store.WorkError) as ctx:
            await runner.send_item("gmail", item_id)
        self.assertEqual(ctx.exception.code, "no_draft")


if __name__ == "__main__":
    unittest.main()
