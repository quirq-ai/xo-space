"""Storm's stream on real turns (ch 09; ch 18 §2 PR 8: "snapshots rebuild
the same blocks as the live stream"): real turns on the fake claude through
the engine, and three views of each compared:

* a client watching live from the start;
* the blocks rebuilt from the stored events alone (history);
* a client that connects mid-turn with a resume point (replay, snapshot,
  live).

All three must be identical. Timers run on the kit's manual clock, so a
coalesced update goes out exactly when a test moves the clock."""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import stream as storm_stream
from services.cowork_agent.storm.engine import Decision
from services.cowork_agent.storm.stream import Filter, present
from tests.fakes import claude
from tests.storm_engine_kit import SID, Desk, Kit, message
from tests.storm_stream_kit import Client, follow, replayed

PUSH = {"command": "git push origin main"}


class StreamCase(unittest.IsolatedAsyncioTestCase):
    maxDiff = None
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.desk = Desk(loop=asyncio.get_running_loop())
        self.tasks: list[asyncio.Task] = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        storm_stream.detach()
        await self.kit.close()

    async def boot(self) -> storm_stream.Bus:
        engine = await self.kit.boot(desk=self.desk)
        return await storm_stream.attach(engine)

    def follow(self, bus: storm_stream.Bus, client: Client, **kw) -> None:
        self.tasks.append(follow(bus, client, **kw))

    def history(self, turn: str) -> list[dict]:
        events = [e for e in self.kit.recorder.committed_events if e.turn_id == turn]
        return present(events)

    async def settle(self) -> None:
        """Let every coalesced update go out."""
        await asyncio.sleep(0.05)
        self.kit.clock.advance(1.0)
        await asyncio.sleep(0.05)


def rich_turn(work: str) -> list[dict]:
    """Thinking, narration, a plan, a file written with its size growing, a
    command, a denied command held for the person, a plan update, a
    sub-agent, and the answer."""
    write = {"file_path": f"{work}/app.py", "content": "print('hi')\n" * 300}
    todos = [{"content": "Write app.py", "status": "in_progress"}, {"content": "Run the tests", "status": "pending"}]
    return [
        *claude.streamed(SID, "m1", [("thinking", "I'm planning the work.\n\nThen I'll write the file."),
                                     ("text", "I'll write app.py, then run the tests.")]),
        *claude.streamed(SID, "m2", [("tool_use", "t_plan", "TodoWrite", {"todos": todos})]),
        claude.tool_result(SID, "t_plan", "ok"),
        *claude.streamed(SID, "m3", [("tool_use", "t_write", "Write", write)]),
        claude.tool_result(SID, "t_write", "File created"),
        *claude.streamed(SID, "m4", [("tool_use", "t_test", "Bash", {"command": "pytest -q",
                                                                    "description": "Run the tests"})]),
        claude.tool_result(SID, "t_test", "3 passed"),
        *claude.streamed(SID, "m5", [("text", "Now I'll push.")]),
        *claude.streamed(SID, "m6", [("tool_use", "t_push", "Bash", PUSH)]),
        claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="t_push"),
        claude.permission_answer("p1", "deny"),
        claude.tool_result(SID, "t_push", "The person said no", is_error=True),
        *claude.streamed(SID, "m7", [("tool_use", "t_plan2", "TodoWrite", {"todos": [
            {**t, "status": "completed"} for t in todos]})]),
        claude.tool_result(SID, "t_plan2", "ok"),
        *claude.streamed(SID, "m8", [("tool_use", "t_task", "Task", {"description": "Read the docs",
                                                                     "subagent_type": "Explore", "prompt": "go"})]),
        claude.task_started(SID, "task_1", tool_use_id="t_task", description="Read the docs", background=False),
        claude.task_progress(SID, "task_1", tokens=120, tool_uses=2),
        claude.tool_call(SID, "sub_1", "Read", {"file_path": f"{work}/README.md"}, message_id="ms",
                         agent_id="task_1"),
        claude.system(SID, "task_notification", task_id="task_1", status="completed", summary="read them"),
        claude.tool_result(SID, "t_task", "The docs say hi"),
        *claude.streamed(SID, "m9", [("text", "All done: app.py is written and 3 tests pass.")]),
        claude.result(SID, "All done: app.py is written and 3 tests pass."),
        claude.idle(SID),
    ]


class LiveHistoryReconnectTests(StreamCase):
    async def test_live_history_and_a_mid_turn_reconnect_show_the_same_blocks(self) -> None:
        kit = self.kit
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID), *rich_turn(str(kit.work))])
        bus = await self.boot()
        live = Client()
        self.follow(bus, live)
        sid = await kit.session()
        await kit.engine.deliver(sid, message(1))

        # Held mid-turn for the person (the engine has recorded everything up
        # to the held call): compare a client that reconnects now.
        await kit.recorder.seen("step.permission", session_id=sid)
        await self.settle()
        started = kit.recorder.of("turn.started", sid)[0]
        turn = started.turn_id
        later = Client()
        self.follow(bus, later, after=started.seq)
        await later.wait(lambda: later.snapshots)
        self.assertEqual(later.open(turn), live.open(turn))
        (snap,) = [t for t in later.snapshots[-1]["turns"] if t["turn_id"] == turn]
        self.assertEqual(snap["held"], [self.desk.opened[0][0]])
        group = next(b for b in later.open(turn) if b["type"] == "activity")
        self.assertEqual(group["rows"][-1]["waiting"], True)          # the push is held for the person

        item, ask = self.desk.opened[0]
        await kit.engine.deliver_decision(item, Decision(allow=False, message="The person said no"), ask.call,
                                          self.desk.record(item, "deny"))
        await kit.recorder.seen("turn.finished", session_id=sid)
        await live.wait(lambda: turn in live.ended)
        await later.wait(lambda: turn in later.ended)

        history = self.history(turn)
        self.assertEqual(live.finished(turn), history)
        # The durable part of the live stream is exactly the replay.
        self.assertEqual(later.durable, [d for d in live.durable if d[0] > started.seq])
        self.assertEqual(later.finished(turn), history)
        self.assertEqual([(b["type"], b.get("header")) for b in history], [
            ("thinking", "Thought for 1s"),
            ("narration", None),
            ("plan", "Plan · 2 of 2 done"),
            ("activity", "Created a file, ran a command"),
            ("narration", None),
            ("activity", "Started a sub-agent, 1 step denied"),
            ("answer", None),
        ])
        thinking, first, plan, files, _, push, answer = history
        self.assertEqual(thinking["steps"], ["I'm planning the work.", "Then I'll write the file."])
        self.assertEqual(first["text"], "I'll write app.py, then run the tests.")
        self.assertEqual([r["line"] for r in files["rows"]], ["Created `app.py`", "Run the tests · done"])
        self.assertEqual(files["rows"][0]["bytes"], 3600)
        self.assertEqual([(r["line"], r["status"]) for r in push["rows"]],
                         [("git push origin main · denied", "denied"), ("Sub-agent: `Read the docs`", "ok")])
        self.assertEqual(answer["text"], "All done: app.py is written and 3 tests pass.")
        self.assertNotIn("Read `README.md`", str(history))          # a sub-agent's own steps are its run's

    async def test_a_stopped_turn_keeps_the_text_that_streamed(self) -> None:
        """Stopped while a file's content and a sentence still stream: the
        half sentence the person watched is kept (the runtime records it as
        it stood) and is the turn's answer, live, in history and after a
        reconnect alike; the write never started, so its row goes."""
        kit = self.kit

        def ev(event: dict) -> dict:
            return {"emit": {"type": "stream_event", "session_id": SID, "event": event}}
        kit.scenario([
            *claude.initialize(), claude.user_message(), claude.init(SID),
            *claude.streamed(SID, "m1", [("text", "I'll write the file.")]),
            ev({"type": "message_start", "message": {"id": "m2", "role": "assistant", "content": []}}),
            ev({"type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "id": "t_w", "name": "Write", "input": {}}}),
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta",
                                                                     "partial_json": '{"file_path": "a.py", '}}),
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta",
                                                                     "partial_json": '"content": "' + "x" * 2048}}),
            ev({"type": "message_start", "message": {"id": "m3", "role": "assistant", "content": []}}),
            ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Half a sen"}}),
            *claude.host_control("interrupt"),
            claude.result(SID, "", subtype="error_during_execution", terminal_reason="aborted_streaming"),
            claude.idle(SID),
        ])
        bus = await self.boot()
        live = Client()
        self.follow(bus, live)
        sid = await kit.session()
        await kit.engine.deliver(sid, message(1))
        started = await kit.recorder.seen("turn.started", session_id=sid)
        turn = started.turn_id
        await kit.until(lambda: any(isinstance(e, c.TextDelta) and e.delta == "Half a sen"
                                    for e in kit.recorder.ephemerals))
        await self.settle()
        self.assertEqual([(b["type"], b.get("header") or b.get("text")) for b in live.open(turn)],
                         [("text", "I'll write the file."), ("activity", "Writing… · 2.0 KB"), ("text", "Half a sen")])

        later = Client()
        self.follow(bus, later, after=started.seq)
        await later.wait(lambda: later.snapshots)
        self.assertEqual(later.open(turn), live.open(turn))

        await kit.engine.stop(sid, by="person")
        await live.wait(lambda: turn in live.ended)
        await later.wait(lambda: turn in later.ended)
        history = self.history(turn)
        self.assertEqual([(b["type"], b.get("text") or b["header"]) for b in history], [
            ("narration", "I'll write the file."), ("activity", "interrupt sent to claude"),   # the runtime's notice
            ("answer", "Half a sen")])
        self.assertEqual(live.finished(turn), history)
        self.assertEqual(later.finished(turn), history)

    async def test_a_sub_agent_shows_its_progress_on_its_row_while_it_runs(self) -> None:
        kit = self.kit
        task = {"description": "Read the docs", "subagent_type": "Explore", "prompt": "go"}
        kit.scenario([
            *claude.initialize(), claude.user_message(), claude.init(SID),
            *claude.streamed(SID, "m1", [("tool_use", "t_task", "Task", task)]),
            claude.task_started(SID, "task_1", tool_use_id="t_task", description="Read the docs", background=False),
            claude.task_progress(SID, "task_1", tokens=120, tool_uses=2),
            claude.can_use_tool("p1", "Bash", PUSH, tool_use_id="sub_push", agent_id="task_1"),
            claude.permission_answer("p1", "deny"),
            claude.system(SID, "task_notification", task_id="task_1", status="completed", summary="read them"),
            claude.tool_result(SID, "t_task", "The docs say hi"),
            *claude.text_turn(SID, "done", message_id="m2"),
            claude.idle(SID),
        ])
        bus = await self.boot()
        live = Client()
        self.follow(bus, live)
        sid = await kit.session()
        await kit.engine.deliver(sid, message(1))
        await kit.recorder.seen("review.opened", session_id=sid)
        await kit.until(lambda: any(isinstance(e, c.RunProgress) for e in kit.recorder.ephemerals))
        await self.settle()
        turn = kit.recorder.of("turn.started", sid)[0].turn_id
        (group,) = live.open(turn)
        (row,) = group["rows"]
        self.assertEqual((row["line"], row["progress"]["tokens"], row["progress"]["tool_calls"]),
                         ("Sub-agent: `Read the docs`", 120, 2))
        item, ask = self.desk.opened[0]
        await kit.engine.deliver_decision(item, Decision(allow=False), ask.call, self.desk.record(item, "deny"))
        await live.wait(lambda: turn in live.ended)
        history = self.history(turn)
        self.assertEqual(live.finished(turn), history)
        self.assertNotIn("progress", str(history))                    # live only
        self.assertEqual(history[0]["rows"][0]["status"], "ok")


class ResumeTests(StreamCase):
    async def write_many(self, count: int) -> None:
        for n in range(count):
            await self.kit.engine.write(lambda w, n=n: w.append("pool", {"n": n}, chat_id="chat_a" if n % 2 else None))
            await asyncio.sleep(0)

    async def test_a_resume_under_concurrent_writes_has_no_gap_and_no_duplicate(self) -> None:
        bus = await self.boot()
        await self.write_many(700)                      # more than a replay page behind
        first = self.kit.recorder.of("pool")[0].seq
        live = Client()
        self.follow(bus, live)
        writer = asyncio.create_task(self.write_many(400))
        await asyncio.sleep(0.01)
        resumed, chat_only = Client(), Client()
        self.follow(bus, resumed, after=first)
        self.follow(bus, chat_only, filter=Filter(chat="chat_a"), after=first)
        await writer
        stored = [e.seq for e in self.kit.recorder.of("pool")]
        await resumed.wait(lambda: resumed.durable and resumed.durable[-1][0] == stored[-1])
        self.assertEqual([d[0] for d in resumed.durable], stored[1:])
        chat_a = [e.seq for e in self.kit.recorder.of("pool") if e.chat_id == "chat_a" and e.seq > first]
        await chat_only.wait(lambda: chat_only.durable and chat_only.durable[-1][0] == chat_a[-1])
        self.assertEqual([d[0] for d in chat_only.durable], chat_a)
        self.assertEqual(resumed.types.count("snapshot"), 1)

    async def test_a_reconnect_goes_live_after_one_catch_up_however_much_is_written_meanwhile(self) -> None:
        """Subscribe first, buffer, replay once: no re-reading to chase the
        store's head (ch 09 §4)."""
        bus = await self.boot()
        await self.write_many(700)
        first = self.kit.recorder.of("pool")[0].seq
        reads = 0
        real = storm_stream.read_page

        async def counted(*args, **kw):
            nonlocal reads
            reads += 1
            return await real(*args, **kw)
        resumed = Client()
        with mock.patch.object(storm_stream, "read_page", counted):
            writers = [asyncio.create_task(self.write_many(40)) for _ in range(16)]
            self.follow(bus, resumed, after=first)
            await resumed.wait(lambda: resumed.snapshots)
            await asyncio.gather(*writers)
        stored = [e.seq for e in self.kit.recorder.of("pool")]
        await resumed.wait(lambda: resumed.durable and resumed.durable[-1][0] == stored[-1])
        self.assertEqual([d[0] for d in resumed.durable], stored[1:])
        self.assertLessEqual(reads, 4)                  # 700 behind, 640 more while it reads: pages of 500

    async def test_a_reconnect_whose_buffer_overflows_while_it_replays_is_a_reset(self) -> None:
        bus = await self.boot()
        await self.write_many(5)
        first = self.kit.recorder.of("pool")[0].seq
        real = storm_stream.read_page

        async def busy(*args, **kw):                    # the store is written while the replay reads it
            await self.write_many(20)
            return await real(*args, **kw)
        resumed = Client()
        with mock.patch.object(storm_stream, "read_page", busy), \
                mock.patch("services.cowork_agent.storm.stream.bus.QUEUE_LIMIT", 10):
            self.follow(bus, resumed, after=first)
            await resumed.wait(lambda: resumed.resets)
        (reset,) = resumed.resets
        self.assertEqual(reset["reason"], "slow")
        self.assertEqual(reset["from_seq"], resumed.last_id)     # everything up to it arrived
        self.assertEqual(await replayed(bus, reset["from_seq"]),
                         [(e.seq, "pool", None) for e in self.kit.recorder.of("pool")
                          if e.seq > int(reset["from_seq"])])

    async def test_a_point_older_than_retention_or_never_reached_is_a_reset(self) -> None:
        bus = await self.boot()
        await self.write_many(3)
        head = self.kit.recorder.of("pool")[-1].seq
        ahead = Client()
        self.follow(bus, ahead, after=head + 50)        # this store never got there: it was replaced
        await ahead.wait(lambda: ahead.resets)
        self.assertEqual(ahead.resets, [{"from_seq": str(head), "reason": "ahead"}])

        self.kit.clock.advance(31 * 24 * 3600)
        old = Client()
        self.follow(bus, old, after=1)
        await old.wait(lambda: old.resets)
        self.assertEqual(old.resets, [{"from_seq": str(head), "reason": "retention"}])
        current = Client()
        self.follow(bus, current, after=head)           # nothing missed: nothing to refuse
        await current.wait(lambda: current.snapshots)
        self.assertEqual(current.resets, [])

    async def test_streams_end_when_the_server_stops(self) -> None:
        bus = await self.boot()
        client = Client()
        self.follow(bus, client)
        await client.wait(lambda: client.snapshots)
        storm_stream.detach()
        await asyncio.wait_for(self.tasks[-1], 5)
        with self.assertRaises(storm_stream.EngineUnavailable):
            storm_stream.current()


class ShutdownSignalTests(StreamCase):
    """uvicorn waits for open responses before the app's shutdown; the
    stream must not be one of them."""

    async def test_an_exit_signal_ends_the_streams_then_reaches_the_servers_own_handler(self) -> None:
        import os
        import signal
        bus = await self.boot()
        client = Client()
        self.follow(bus, client)
        await client.wait(lambda: client.snapshots)
        seen: list[int] = []

        def server(signum, frame) -> None:              # uvicorn's handle_exit, as far as this test goes
            seen.append(signum)
        saved = {sig: signal.signal(sig, server) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            restore = storm_stream.end_streams_on_exit_signal(asyncio.get_running_loop())
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(self.tasks[-1], 5)
            self.assertEqual(seen, [signal.SIGTERM])
            self.assertTrue(bus.closed)
            restore()
            self.assertEqual({sig: signal.getsignal(sig) for sig in saved}, {sig: server for sig in saved})
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)

    def test_after_its_loop_closed_the_signal_only_reaches_the_handler_before_it(self) -> None:
        import signal
        seen: list[int] = []
        loop = asyncio.new_event_loop()
        saved = signal.signal(signal.SIGINT, lambda signum, frame: seen.append(signum))
        try:
            restore = storm_stream.end_streams_on_exit_signal(loop)
            loop.close()
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)    # no "Event loop is closed"
            self.assertEqual(seen, [signal.SIGINT])
            restore()
        finally:
            signal.signal(signal.SIGINT, saved)


class ExactlyOnceTests(StreamCase):
    """ch 09 §4: a reconnect loses nothing and gets nothing twice, wherever
    the connection dropped and however the replay's pages fall."""

    async def finished_rich_turn(self) -> tuple[storm_stream.Bus, Client, str]:
        kit = self.kit
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID), *rich_turn(str(kit.work))])
        bus = await self.boot()
        live = Client()
        self.follow(bus, live)
        sid = await kit.session()
        await kit.engine.deliver(sid, message(1))
        await kit.recorder.seen("step.permission", session_id=sid)
        turn = kit.recorder.of("turn.started", sid)[0].turn_id
        await kit.engine.write(lambda w: [w.append("step.notice", {"code": "api_retry", "message": f"retry {n}"},
                                                   session_id=sid, turn_id=turn) for n in range(3)])
        item, ask = self.desk.opened[0]
        await kit.engine.deliver_decision(item, Decision(allow=False), ask.call, self.desk.record(item, "deny"))
        await live.wait(lambda: turn in live.ended)
        await self.settle()
        return bus, live, turn

    async def test_a_resume_after_any_frame_at_any_page_size_gets_exactly_the_rest(self) -> None:
        bus, live, _turn = await self.finished_rich_turn()
        shared = {seq for seq, *_ in live.durable if sum(d[0] == seq for d in live.durable) > 1}
        self.assertGreaterEqual(len(shared), 5)             # stored events that give several frames
        for page in (1, 2, 3, 7, 500):
            with self.subTest(page=page), mock.patch.object(storm_stream, "PAGE_LIMIT", page):
                for i, ident in enumerate(live.ids):
                    rest = await replayed(bus, ident)
                    self.assertEqual(live.durable[:i + 1] + rest, live.durable, f"after {ident}, pages of {page}")
                self.assertEqual(await replayed(bus, 0), live.durable)

    async def test_a_resume_after_a_whole_seq_gets_everything_after_it(self) -> None:
        bus, live, _turn = await self.finished_rich_turn()
        for seq in sorted({d[0] for d in live.durable}):
            with self.subTest(after_seq=seq):
                self.assertEqual(await replayed(bus, seq), [d for d in live.durable if d[0] > seq])
