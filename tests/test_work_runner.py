"""``services/work/runner.py``: one session per Inbox work item, over a fake
dispatcher stream (docs/work-and-workitems.md section 18). The stream seam
(``runner._open_stream``) is patched, so no runtime is spawned, and the
inbox refresh is stubbed so no feeder runs; everything else (the project
scaffold, the claims, the sidecars, the work item file, the timeline lines)
is real, in a temp state root."""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.scopes import VisualizerScope
from services.work import inbox_view, items, runner, service, store

from tests.inbox_harness import HANDLED, OUTCOME, ROOT, LoopRoot, connection_fact, stream_of


class ParseTests(unittest.TestCase):
    def test_the_last_json_block_is_the_outcome(self) -> None:
        out = runner.parse_outcome('```json\n{"kind": "fyi", "summary": "first"}\n```\ntext\n' + OUTCOME)
        self.assertEqual((out["kind"], out["draft"], out["acted"]), ("reply_drafted", "reply.md", []))
        self.assertTrue(out["at"].endswith("Z"))
        for bad in ("no block", "```json\n[1]\n```", '```json\n{"kind": "odd", "summary": "s"}\n```',
                    '```json\n{"kind": "fyi"}\n```', '```json\n{"kind": "fyi", "summary": "s", "draft": "../x"}\n```',
                    '```json\n{"kind": "task_proposed", "summary": "s", "task": {}}\n```',
                    '```json\n{"kind": "fyi", "summary": "s", "acted": "sent"}\n```'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    runner.parse_outcome(bad)

    def test_the_runner_names_no_agent_and_uses_the_dispatcher_seam(self) -> None:
        src = (ROOT / "services" / "work" / "runner.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"\b(openclaw|hermes|claude_code|codex)\b")
        self.assertIn("AgentDispatcher(session[\"runtime\"])", src)
        self.assertNotIn("\u2014", src)


class StartTests(LoopRoot):
    async def test_auto_mode_starts_new_items_and_the_outcome_lands_on_the_work_item(self) -> None:
        project_id, wid = self.ingest(connection_fact("m1"))
        items.write_policy("connections", {"sessions": {"mode": "auto"}})
        gate = asyncio.Event()
        with patch.object(runner, "_open_stream", stream_of(OUTCOME, gate=gate)) as opened:
            summary = await runner.tick()
            self.assertEqual(summary["started"], 1)
            self.assertEqual(runner.running("connections"), [(project_id, wid)])
            row = service.inbox_row(project_id, wid)
            self.assertEqual((row["state"], row["claim"]["session_id"] is not None, row["claim"]["runtime"]), ("running", True, "sample_agent"))
            gate.set()
            await self.finish()
        call = opened.calls[0]
        self.assertEqual((call["runtime"], call["agent_type"], call["agent_id"], call["is_new"]), ("sample_agent", "inbox-item", project_id, True))
        self.assertIn("Invoice question", call["prompt"])
        self.assertIn("Hi, the June invoice shows 12 seats.", call["prompt"])
        self.assertIn("the gmail connection", call["prompt"])
        self.assertIn("Draft only", call["prompt"])
        self.assertIn(f"items/{wid}", call["prompt"])
        session = items.read_session(project_id, wid)
        self.assertEqual((session["native_session_id"], session["exit"]["status"], session["attempt"], session["manual"]), ("native-1", "ok", 1, False))
        self.assertEqual(items.read_outcome(project_id, wid)["kind"], "reply_drafted")
        record = VisualizerScope(project_id).get_workitem(wid)
        self.assertEqual(record["links"]["session_ids"], [session["session_id"]])
        self.assertEqual(record["status"], "open", "a drafted reply waits for the person")
        self.assertEqual(VisualizerScope(project_id).read_claims(), {}, "the claim is released when the turn ends")
        self.assertEqual(service.inbox_row(project_id, wid)["state"], "waiting")
        self.assertTrue(items.workbench_dir(project_id, wid).is_dir())
        types = [t for t, _s in self.space_types()]
        for wanted in ("workitem.created", "inbox.item.started", "workitem.claimed", "workitem.released", "inbox.item.finished"):
            self.assertIn(wanted, types)
        self.assertEqual(await runner.tick(), {"created": 0, "started": 0, "orphaned": 0, "swept": 0}, "nothing new: nothing starts again")

    async def test_handled_closes_the_work_item(self) -> None:
        project_id, wid = self.ingest(connection_fact("news"))
        with patch.object(runner, "_open_stream", stream_of(HANDLED)):
            await runner.start_item(project_id, wid)
            await self.finish()
        record = VisualizerScope(project_id).get_workitem(wid)
        self.assertEqual((record["status"], record["state_reason"]), ("closed", "completed"))
        self.assertEqual(service.inbox_row(project_id, wid)["state"], "closed")
        self.assertIn(("inbox.item.finished", "handled"), self.space_types())
        with self.assertRaises(store.WorkError) as ctx:
            await runner.start_item(project_id, wid, retry=True)
        self.assertEqual(ctx.exception.code, "item_closed")

    async def test_manual_sections_wait_kinds_narrow_and_caps_hold(self) -> None:
        project_id, a = self.ingest(connection_fact("a", minutes_ago=30))
        _, b = self.ingest(connection_fact("b", minutes_ago=20))
        _, c = self.ingest(connection_fact("c", minutes_ago=10, toolkit="googlecalendar", kind="upcoming"))
        with patch.object(runner, "_open_stream", stream_of(OUTCOME, hang=True)):
            self.assertEqual((await runner.tick())["started"], 0, "the sample pins connections to manual")
            items.write_policy("connections", {"sessions": {"mode": "auto", "kinds": ["gmail.unread"], "max_concurrent": 1}})
            self.assertEqual((await runner.tick())["started"], 1)
            self.assertEqual(runner.running(), [(project_id, a)], "oldest first, one at a time")
            with self.assertRaises(store.WorkError) as ctx:
                await runner.start_item(project_id, b)
            self.assertEqual(ctx.exception.code, "cap_reached")
            with self.assertRaises(store.WorkError) as ctx:
                await runner.start_item(project_id, a)
            self.assertEqual(ctx.exception.code, "session_exists")
            for task in list(runner._running.values()):
                task.cancel()
            await self.finish()
        self.assertEqual(service.inbox_row(project_id, a)["state"], "failed", "a cancelled session is a failed one")
        items.write_policy("connections", {"sessions": {"mode": "auto", "kinds": ["gmail.unread"], "max_concurrent": 2}})
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            self.assertEqual((await runner.tick())["started"], 1, "b starts; a already had a session, c is not a listed kind")
            await self.finish()
        self.assertEqual(service.inbox_row(project_id, c)["state"], "new")
        items.write_policy("connections", {"sessions": {"mode": "off"}})
        with self.assertRaises(store.WorkError) as ctx:
            await runner.start_item(project_id, c)
        self.assertEqual(ctx.exception.code, "sessions_off")

    async def test_after_poll_starts_connection_items_at_once(self) -> None:
        project_id, wid = self.ingest(connection_fact("m1"))
        items.write_policy("connections", {"sessions": {"mode": "auto"}})
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            await runner._after_poll("gmail")
            self.assertEqual(runner.running(), [(project_id, wid)])
            await self.finish()


class ReplyTests(LoopRoot):
    async def test_reply_resumes_the_session_and_a_first_message_starts_one(self) -> None:
        project_id, wid = self.ingest(connection_fact("m1"))
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)) as opened:
            await runner.reply_item(project_id, wid, "Keep it short.")
            await self.finish()
        self.assertEqual(opened.calls[0]["is_new"], True)
        self.assertIn("The person adds, before you start:\nKeep it short.", opened.calls[0]["prompt"])
        session_id = items.read_session(project_id, wid)["session_id"]
        with patch.object(runner, "_open_stream", stream_of("Shortened it, no new outcome.")) as opened:
            result = await runner.reply_item(project_id, wid, "Shorter still?")
            self.assertEqual(result["session_id"], session_id)
            self.assertEqual(service.inbox_row(project_id, wid)["state"], "running")
            await self.finish()
        call = opened.calls[0]
        self.assertEqual((call["is_new"], call["session_id"]), (False, session_id))
        self.assertTrue(call["prompt"].startswith("Shorter still?"))
        session = items.read_session(project_id, wid)
        self.assertEqual((session["exit"]["status"], session["attempt"]), ("ok", 1), "a follow-up answer need not restate the outcome")
        self.assertIsNotNone(session["resumed_at"])
        self.assertEqual(items.read_outcome(project_id, wid)["kind"], "reply_drafted", "the outcome stays")
        self.assertEqual(service.inbox_row(project_id, wid)["state"], "waiting")
        self.assertIn(("inbox.item.finished", "replied"), self.space_types())
        for bad in ("", "   ", "x" * (runner.REPLY_TEXT_MAX + 1)):
            with self.assertRaises(store.WorkError):
                await runner.reply_item(project_id, wid, bad)

    async def test_send_needs_act_and_a_drafted_reply(self) -> None:
        project_id, wid = self.ingest(connection_fact("m1"))
        with self.assertRaises(store.WorkError) as ctx:
            await runner.send_item(project_id, wid)
        self.assertEqual(ctx.exception.code, "act_not_allowed")
        items.write_policy("connections", {"sessions": {"act": True}})
        with self.assertRaises(store.WorkError) as ctx:
            await runner.send_item(project_id, wid)
        self.assertEqual(ctx.exception.code, "no_draft")
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            await runner.start_item(project_id, wid)
            await self.finish()
        self.assertTrue(service.inbox_item(project_id, wid)["can_send"])
        with patch.object(runner, "_open_stream", stream_of(HANDLED)) as opened:
            await runner.send_item(project_id, wid)
            await self.finish()
        self.assertIn("The person approved your draft at reply.md", opened.calls[0]["prompt"])
        self.assertEqual(items.read_outcome(project_id, wid)["kind"], "handled")
        self.assertEqual(service.inbox_row(project_id, wid)["state"], "closed")


class FailureTests(LoopRoot):
    async def test_error_timeout_and_retry(self) -> None:
        project_id, wid = self.ingest(connection_fact("m1"))
        with patch.object(runner, "_open_stream", stream_of("half an answer", error="boom")):
            await runner.start_item(project_id, wid)
            await self.finish()
        session = items.read_session(project_id, wid)
        self.assertEqual((session["exit"]["status"], session["exit"]["message"]), ("error", "boom"))
        self.assertEqual(service.inbox_row(project_id, wid)["state"], "failed")
        self.assertEqual(VisualizerScope(project_id).read_claims(), {})
        self.assertIn(("inbox.item.failed", "error"), self.space_types())
        with self.assertRaises(store.WorkError) as ctx:
            await runner.start_item(project_id, wid)
        self.assertEqual(ctx.exception.code, "session_exists")
        items.write_policy("connections", {"sessions": {"timeout_s": 30}})
        with patch.object(runner, "_open_stream", stream_of(OUTCOME, hang=True)), \
             patch.object(runner.asyncio, "wait_for", side_effect=asyncio.TimeoutError):
            await runner.start_item(project_id, wid, retry=True)
            await self.finish()
        session = items.read_session(project_id, wid)
        self.assertEqual((session["exit"]["status"], session["attempt"]), ("timeout", 2))
        with patch.object(runner, "_open_stream", stream_of("no outcome block here")):
            await runner.start_item(project_id, wid, retry=True)
            await self.finish()
        self.assertEqual(items.read_session(project_id, wid)["exit"]["status"], "error")
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            await runner.start_item(project_id, wid, retry=True)
            await self.finish()
        self.assertEqual((items.read_session(project_id, wid)["attempt"], service.inbox_row(project_id, wid)["state"]), (4, "waiting"))
        with self.assertRaises(store.WorkError) as ctx:
            await runner.start_item(project_id, wid, retry=True)
        self.assertEqual(ctx.exception.code, "not_failed")

    async def test_a_session_left_running_by_a_restart_is_harvested(self) -> None:
        project_id, wid = self.ingest(connection_fact("m1"))
        items.write_session(project_id, wid, {"session_id": "ghost", "runtime": "sample_agent", "project_id": project_id,
                                              "workitem_id": wid, "section": "connections", "started_at": "2026-09-20T09:00:00Z",
                                              "ended_at": None, "exit": None, "attempt": 1, "manual": False})
        VisualizerScope(project_id).claim_workitem(wid, session_id="ghost", runtime="sample_agent", started_at="2026-09-20T09:00:00Z")
        self.assertEqual(service.inbox_row(project_id, wid)["state"], "failed", "a stale claim is not live")
        summary = await runner.tick()
        self.assertEqual(summary["orphaned"], 1)
        session = items.read_session(project_id, wid)
        self.assertEqual(session["exit"]["status"], "orphaned")
        self.assertEqual(VisualizerScope(project_id).read_claims(), {})
        self.assertIn(("inbox.item.failed", "orphaned"), self.space_types())


class ServiceTests(LoopRoot):
    async def test_detail_sections_archive_and_reopen(self) -> None:
        project_id, wid = self.ingest(connection_fact("m1"))
        detail = service.inbox_item(project_id, wid)
        self.assertEqual((detail["row"]["state"], detail["session"], detail["outcome"], detail["running"]), ("new", None, None, False))
        self.assertEqual(detail["transcript"], {"session_id": None, "native_session_id": None})
        self.assertEqual(detail["policy"]["sessions"]["mode"], "manual")
        self.assertEqual(detail["workbench"]["path"], f"items/{wid}")
        with patch.object(runner, "_open_stream", stream_of(OUTCOME)):
            await runner.start_item(project_id, wid)
            with self.assertRaises(store.WorkError) as ctx:
                service.archive_inbox_item(project_id, wid)
            self.assertEqual(ctx.exception.code, "session_running")
            await self.finish()
        detail = service.inbox_item(project_id, wid)
        self.assertEqual(detail["transcript"]["native_session_id"], "native-1")
        self.assertEqual(detail["workitem"]["links"]["session_ids"], [detail["session"]["session_id"]])
        sections = service.inbox_sections()["sections"]
        connections = next(s for s in sections if s["id"] == "connections")
        self.assertEqual((connections["counts"]["waiting"], connections["running"], connections["policy"]["sessions"]["mode"]), (1, 0, "manual"))
        closed = service.archive_inbox_item(project_id, wid)
        self.assertEqual((closed["state"], closed["state_reason"]), ("closed", "completed"))
        with self.assertRaises(store.WorkError):
            service.archive_inbox_item(project_id, wid, reason="later")
        reopened = service.reopen_inbox_item(project_id, wid)
        self.assertEqual((reopened["state"], reopened["state_reason"]), ("waiting", "reopened"))
        listing = service.inbox_rows(section="connections", state="waiting")
        self.assertEqual([r["id"] for r in listing["rows"]], [wid])
        self.assertEqual(listing["runner"], {"enabled": True})

    async def test_the_work_page_carries_the_items(self) -> None:
        project_id, wid = self.ingest(connection_fact("m1"))
        with patch.object(runner, "_open_stream", stream_of(HANDLED)):
            await runner.start_item(project_id, wid)
            await self.finish()
        page = service.inbox_page()
        completed = [r for r in page["completed"] if r["kind"] == "item"]
        self.assertEqual([r["ref"]["workitem_id"] for r in completed], [wid])
        self.assertEqual(page["running"], [])
        connections = next(s for s in page["sections"] if s["section"] == "connections")
        self.assertEqual(connections["counts"]["closed"], 1)
        _, asked = self.ingest(connection_fact("m2"))
        with patch.object(runner, "_open_stream", stream_of(OUTCOME.replace("reply_drafted", "needs_you").replace('"question": null', '"question": "Which seats?"'))):
            await runner.start_item(project_id, asked)
            await self.finish()
        decisions = [d for d in service.attention_items()["items"] if d["reason"].startswith("item_")]
        self.assertEqual([(d["reason"], d["ref"]["workitem_id"]) for d in decisions], [("item_question", asked)])
        self.assertEqual(decisions[0]["detail"], "Which seats?")


if __name__ == "__main__":
    unittest.main()
