"""A thread's one-sentence summary (v2): written when its turn ends from the
thread's own record, kept to 90 characters, rewritten only when there is
something new, and asked of an agent whose oneshot is isolated."""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.chats import summaries
from services.cowork_agent.storm.chats.summaries import Summaries, clean
from services.cowork_agent.storm.control import founder
from services.cowork_agent.storm.engine import Writes
from services.cowork_agent.storm.engine.engine import pool_account, pool_agent
from services.cowork_agent.storm.engine.ports import iso
from tests.storm_comms_kit import CHOICE, CommsTestCase


class CleanTests(unittest.TestCase):
    def test_one_line_at_most_90_characters_cut_at_a_word(self) -> None:
        self.assertEqual(clean('  "Fixed the login\n redirect"  '), "Fixed the login redirect")
        long = "Rewrote the billing export so that every invoice line carries its tax region and currency code"
        got = clean(long)
        self.assertLessEqual(len(got), 90)
        self.assertTrue(got.endswith("…"))
        self.assertTrue(long.startswith(got[:-1]))
        self.assertIsNone(clean("   "))


class SummaryTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.asked: list[tuple[str | None, c.OneshotRequest]] = []
        self.answer = "Wrote the README and opened PR #37"
        self.summaries = Summaries(complete=self.complete)
        self.summaries.start(self.kit.engine)
        self.addAsyncCleanup(self.summaries.stop)
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        root = await self.owner_message(self.chat, "Please write the README")
        started = await self.call(await self.token(self.pc), "start_thread_session", message_id=root["id"],
                                  title="README", instructions="x", **CHOICE)
        self.ts, self.sid = root["id"], started["session_id"]
        self.thread_token = await self.token(self.sid)

    async def complete(self, agent, request: c.OneshotRequest) -> c.OneshotResult:
        self.asked.append((agent, request))
        return c.OneshotResult(text=None, structured={"summary": self.answer})

    async def turn(self) -> None:
        n = len(self.rows("SELECT id FROM turns"))

        def write(w: Writes) -> None:
            w.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at, ended_at) VALUES "
                      "(?, ?, 0, 'person', 'finished', ?, ?)", (f"turn_{n}", self.sid, w.now, w.now))
            w.append("turn.finished", {}, chat_id=self.chat["id"], session_id=self.sid, thread_ts=self.ts,
                     turn_id=f"turn_{n}")
        await self.kit.engine.write(write)

    async def settled(self) -> None:
        await self.kit.until(lambda: not self.summaries._tasks)

    def thread(self) -> dict:
        (summary, summary_at), = self.rows("SELECT summary, summary_at FROM threads WHERE ts = ?", (self.ts,))
        return {"summary": summary, "summary_at": summary_at}

    async def test_a_turns_end_writes_the_summary_from_the_threads_record(self) -> None:
        await self.call(self.thread_token, "reply", text="Done: README written, PR #37 opened.")
        await self.call(self.thread_token, "update_status", text="✓ Write README")
        await self.turn()
        await self.settled()
        self.assertEqual(self.thread()["summary"], self.answer)
        ((agent, request),) = self.asked
        self.assertEqual(agent, "claude_code")              # the thread's own agent
        for words in ("Title: README", "Please write the README", "✓ Write README", "PR #37 opened"):
            self.assertIn(words, request.prompt)
        self.assertEqual((request.model, request.output_schema["required"]), (None, ["summary"]))
        view = (await self.http.get(f"/api/chat/chats/{self.chat['id']}/threads/{self.ts}")).json()
        self.assertEqual(view["summary"], self.answer)
        events = [e.data for e in self.kit.recorder.of("thread") if e.data.get("summary")]
        self.assertEqual([e["summary"] for e in events], [self.answer])

    async def test_no_summary_calls_a_model_on_a_pool_that_holds_background_work(self) -> None:
        """A summary is a model call outside any session: on a pool near its
        limit, held by the paid gate or closed, it would be one more call,
        billed on paid usage at worst. It is skipped then, and written at
        the next turn's end once the pool admits background work again."""
        engine = self.kit.engine
        pid = await engine._current_pool("claude_code")
        account = (pool_agent(pid), pool_account(pid))
        resets = iso(int(self.kit.clock.time() // 60) * 60 + 3600)
        for state, used, status in (("warn", 0.85, "ok"), ("paid", 0.97, "ok"), ("closed", 1.0, "limited")):
            with self.subTest(state=state):
                self.asked.clear()
                engine.advisory.record_usage(*account, c.RateLimit(
                    session="s", ts="t", seq=1, status=status, window="weekly", resets_at=resets, utilization=used,
                    paid=c.PaidUsage(possible=state != "closed")))
                self.assertEqual(engine.pool(*account).state, state)
                await self.call(self.thread_token, "reply", text=f"Still working ({state}).")
                await self.turn()
                await self.settled()
                self.assertEqual(self.asked, [])
                self.assertIsNone(self.thread()["summary"])
        self.kit.clock.advance(3600)                                    # the window resets
        await self.turn()
        await self.settled()
        self.assertEqual(self.thread()["summary"], self.answer)

    async def test_no_summary_calls_a_model_where_the_founder_holds_the_work(self) -> None:
        engine = self.kit.engine
        for scope, scope_id in (("thread", f"{self.chat['id']}/{self.ts}"), ("chat", self.chat["id"]),
                                ("space", "")):
            with self.subTest(scope=scope):
                await founder.pause(engine, scope, scope_id)
                await self.turn()
                await self.settled()
                self.assertEqual(self.asked, [])
                await founder.resume(engine, scope, scope_id)
        await self.turn()
        await self.settled()
        self.assertEqual(self.thread()["summary"], self.answer)

    async def test_resolving_rewrites_only_after_a_new_turn(self) -> None:
        await self.turn()
        await self.settled()
        self.kit.clock.advance(5)
        await self.http.patch(f"/api/chat/chats/{self.chat['id']}/threads/{self.ts}", json={"resolved": True})
        await self.settled()
        self.assertEqual(len(self.asked), 1)                 # nothing new since the summary
        await self.http.patch(f"/api/chat/chats/{self.chat['id']}/threads/{self.ts}", json={"resolved": False})
        await self.turn()
        await self.settled()
        self.assertEqual(len(self.asked), 2)
        self.assertIn("Its summary so far", self.asked[1][1].prompt)

    async def test_turns_ending_while_one_is_written_are_summarized_once_after(self) -> None:
        gate = asyncio.Event()
        original = self.complete

        async def slow(agent, request):
            await gate.wait()
            return await original(agent, request)
        self.summaries._complete = slow
        for _ in range(3):
            await self.turn()
        await asyncio.sleep(0.05)
        gate.set()
        await self.settled()
        self.assertEqual(len(self.asked), 2)

    async def test_no_isolated_oneshot_writes_no_summary(self) -> None:
        emulated = types.SimpleNamespace(oneshot=lambda: types.SimpleNamespace(
            check=lambda: c.SupportReport(items=(c.SupportItem(feature="isolation", support="emulated", mechanism="a temp folder",
                                                       enforced_by="host"),)),
            complete=mock.AsyncMock()))
        self.summaries._complete = None
        with mock.patch.object(summaries, "try_load_capability", return_value=emulated):
            self.assertIsNone(await self.summaries.summarize(self.chat["id"], self.ts))   # no turn yet
            await self.turn()
            await self.settled()
            self.assertIsNone(await self.summaries.summarize(self.chat["id"], self.ts))
        emulated.oneshot().complete.assert_not_awaited()
        self.assertIsNone(self.thread()["summary"])

    async def test_the_oneshots_process_is_registered_until_it_is_reaped(self) -> None:
        handle = await summaries._Launcher(self.kit.engine).spawn(["sleep", "0.2"], cwd="/", env={}, label="t")
        self.assertEqual(self.rows("SELECT session_id, argv0 FROM procs WHERE pid = ?", (handle.pid,)),
                         [(None, "sleep")])
        await self.kit.until(lambda: not self.rows("SELECT 1 FROM procs WHERE pid = ?", (handle.pid,)))


if __name__ == "__main__":
    unittest.main()
