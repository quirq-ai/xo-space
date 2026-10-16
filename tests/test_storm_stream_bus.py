"""The bus (ch 09 §3, §4) on its own: coalescing on an injectable clock,
file sizes at most four a second, a slow subscriber (live frames dropped
first, then ``reset``; the queue never grows past its bound), heartbeats,
and filters. Events are fed to the bus as the engine would."""

from __future__ import annotations

import asyncio
import unittest

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.stream import QUEUE_LIMIT, Bus, Filter
from services.cowork_agent.storm.stream.bus import COALESCE_S, HEARTBEAT_S
from services.cowork_agent.storm.stream.frames import Frame
from services.cowork_agent.storm.stream.presenter import Streams
from tests.storm_engine_kit import ManualClock
from tests.storm_stream_kit import Client

SESSION, CHAT, TURN, TS = "cse_1", "chat_1", "turn_1", "2026-10-15T10:00:00Z"


class StubSource:
    """The engine side of the seam, as the bus sees it."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock

    def listen(self, listener) -> None:
        pass

    def streams(self, session_id) -> Streams:
        return Streams(thinking_text=True, input_bytes=True, output=True)

    def held(self, session_id) -> list[str]:
        return []


class Feed:
    """Stored and live events for one turn, numbered as the engine would."""

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.seq = 0
        self.n = 0

    def stored(self, type_: str, data: dict | None = None, *, turn: str | None = TURN,
               session: str = SESSION, chat: str = CHAT) -> StoredEvent:
        self.seq += 1
        event = StoredEvent(self.seq, TS, type_, data or {}, chat_id=chat, session_id=session, turn_id=turn)
        self.bus.committed([event])
        return event

    def live(self, cls: type[c.Event], **fields) -> None:
        self.n += 1
        self.bus.ephemeral(cls(session=SESSION, ts=TS, seq=self.n, turn_id=TURN, **fields))


class BusCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = ManualClock()
        self.bus = Bus(StubSource(self.clock))
        self.feed = Feed(self.bus)
        self.tasks: list[asyncio.Task] = []

    async def asyncTearDown(self) -> None:
        self.bus.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def reader(self, filter: Filter = Filter()) -> Client:
        client = Client()
        subscriber = self.bus.subscribe(filter)
        subscriber.go_live(self.bus.processed)

        async def run() -> None:
            async for frame in subscriber.frames():
                client.apply(frame)
        self.tasks.append(asyncio.create_task(run()))
        return client

    async def turn(self) -> None:
        self.feed.stored("turn.started", {"origin": "person"})
        await asyncio.sleep(0)


class CoalescingTests(BusCase):
    async def test_deltas_go_out_together_after_the_coalescing_window(self) -> None:
        client = self.reader()
        await self.turn()
        for piece in ("Hel", "lo, ", "wor", "ld"):
            self.feed.live(c.TextDelta, block_id="b1", delta=piece)
        await asyncio.sleep(0)
        self.assertEqual(client.types.count("block.updated"), 0)
        self.clock.advance(COALESCE_S * 0.6)
        await asyncio.sleep(0)
        self.assertEqual(client.types.count("block.updated"), 0)
        self.clock.advance(COALESCE_S * 0.5)
        await asyncio.sleep(0)
        self.assertEqual(client.types.count("block.updated"), 1)
        self.assertEqual(client.open(TURN)[0]["text"], "Hello, world")

    async def test_a_file_size_goes_out_at_most_four_times_a_second(self) -> None:
        client = self.reader()
        await self.turn()
        self.feed.stored("step.tool", {"call_id": "w1", "tool": "Write", "kind": "create_file", "title": "Write `a`",
                                       "target": "/w/a.py"})
        self.clock.advance(1)
        await asyncio.sleep(0)
        before = client.types.count("block.updated")
        for n in range(1, 41):                          # a delta every 25 ms for one second
            self.feed.live(c.ToolInputDelta, call_id="w1", bytes_so_far=n * 100)
            self.clock.advance(0.025)
            await asyncio.sleep(0)
        self.clock.advance(1)
        await asyncio.sleep(0)
        sizes = client.types.count("block.updated") - before
        self.assertLessEqual(sizes, 5)                  # 4 a second, plus the last one held back
        self.assertGreaterEqual(sizes, 4)
        self.assertEqual(client.open(TURN)[0]["rows"][0]["bytes"], 4000)
        self.assertEqual(client.open(TURN)[0]["rows"][0]["line"], "Writing `a.py` · 3.9 KB")

    async def test_pending_updates_go_out_before_the_block_finishes(self) -> None:
        client = self.reader()
        await self.turn()
        self.feed.live(c.TextDelta, block_id="b1", delta="All done")
        self.feed.stored("step.text", {"block_id": "b1", "text": "All done"})
        self.feed.stored("turn.finished", {"status": "finished"})
        await asyncio.sleep(0)
        self.assertEqual([t for t in client.types if t.startswith(("block", "turn"))],
                         ["turn.started", "block.started", "block.updated", "block.started", "block.finished",
                          "turn.finished"])
        self.assertEqual(client.finished(TURN), [{"id": "b1", "type": "answer", "index": 0, "text": "All done"}])
        self.assertEqual(self.bus.turns.running, {})    # the snapshot is dropped with the turn


class BackPressureTests(BusCase):
    async def test_a_stalled_subscriber_drops_live_frames_then_catches_up_from_a_snapshot(self) -> None:
        stalled = self.bus.subscribe(Filter())
        stalled.go_live(0)
        await self.turn()
        text = ""
        for n in range(QUEUE_LIMIT + 300):              # one coalesced update each
            piece = f"{n} "
            text += piece
            self.feed.live(c.TextDelta, block_id="b1", delta=piece)
            self.clock.advance(COALESCE_S)
            self.assertLessEqual(stalled.backlog, QUEUE_LIMIT)
        self.assertGreater(stalled.dropped, 0)

        client = Client()
        frames = stalled.frames()
        while not client.snapshots or client.types[-1] != "snapshot":
            client.apply(await anext(frames))
        self.assertEqual(client.open(TURN)[0]["text"], text)         # nothing lost, nothing twice
        self.assertIn("turn.started", client.types)                  # stored frames are never dropped

    async def test_a_stored_frame_that_cant_be_queued_ends_the_stream_with_reset(self) -> None:
        stalled = self.bus.subscribe(Filter())
        stalled.go_live(0)
        frames = stalled.frames()
        first = self.feed.stored("pool", {"state": "open"}, turn=None)
        self.assertEqual((await anext(frames)).type, "snapshot")
        self.assertEqual((await anext(frames)).seq, first.seq)
        for _ in range(QUEUE_LIMIT + 1):
            self.feed.stored("pool", {"state": "open"}, turn=None)
            self.assertLessEqual(stalled.backlog, QUEUE_LIMIT)
        reset = await anext(frames)
        self.assertEqual((reset.type, reset.data), ("reset", {"from_seq": first.seq, "reason": "slow"}))
        with self.assertRaises(StopAsyncIteration):
            await anext(frames)


class HeartbeatTests(BusCase):
    async def test_a_heartbeat_after_fifteen_seconds_of_silence(self) -> None:
        client = self.reader()
        await asyncio.sleep(0)
        self.clock.advance(HEARTBEAT_S - 1)
        self.feed.stored("pool", {"state": "open"}, turn=None)        # not silent: the count starts again
        await asyncio.sleep(0)
        self.clock.advance(HEARTBEAT_S - 1)
        await asyncio.sleep(0)
        self.assertEqual(client.heartbeats, 0)
        self.clock.advance(1)
        await asyncio.sleep(0)
        self.assertEqual(client.heartbeats, 1)
        self.clock.advance(HEARTBEAT_S)
        await asyncio.sleep(0)
        self.assertEqual(client.heartbeats, 2)

    async def test_closing_the_bus_ends_every_stream(self) -> None:
        subscriber = self.bus.subscribe(Filter())
        subscriber.go_live(0)
        frames = [f async for f in _until_closed(self.bus, subscriber)]
        self.assertEqual([f.type for f in frames], ["snapshot"])


async def _until_closed(bus: Bus, subscriber) -> list[Frame]:
    asyncio.get_running_loop().call_soon(bus.close)
    async for frame in subscriber.frames():
        yield frame


class FilterTests(BusCase):
    def test_kinds_name_a_type_or_its_family_and_control_frames_always_pass(self) -> None:
        f = Filter.parse(kinds="turn, review.opened")
        self.assertTrue(f.wants_kind("turn.finished"))
        self.assertTrue(f.wants_kind("review.opened"))
        self.assertFalse(f.wants_kind("review.decided"))
        self.assertFalse(f.wants_kind("block.started"))
        self.assertTrue(f.wants_kind("heartbeat"))
        self.assertFalse(f.wants_blocks)
        self.assertTrue(Filter.parse(kinds="").wants_blocks)

    async def test_a_subscriber_sees_only_its_chat_or_session(self) -> None:
        mine, theirs, everything = self.reader(Filter(chat=CHAT)), self.reader(Filter(session="cse_other")), \
            self.reader()
        await self.turn()
        self.feed.stored("message", {"text": "hi"}, turn=None, chat="chat_2", session="cse_other")
        self.feed.live(c.TextDelta, block_id="b1", delta="x")
        self.clock.advance(1)
        await asyncio.sleep(0)
        self.assertEqual([t for t in mine.types if t != "snapshot"], ["turn.started", "block.started",
                                                                      "block.updated"])
        self.assertEqual([t for t in theirs.types if t != "snapshot"], ["message"])
        self.assertEqual([t for t in everything.types if t != "snapshot"], ["turn.started", "message",
                                                                            "block.started", "block.updated"])
        self.assertEqual(theirs.snapshots[0]["turns"], [])


class FailureTests(BusCase):
    async def test_an_event_the_presenter_cant_read_is_logged_and_the_stream_goes_on(self) -> None:
        client = self.reader()
        await self.turn()
        with self.assertLogs("services.cowork_agent.storm.stream.bus", "ERROR"):
            bad = self.feed.stored("step.tool", {"kind": "read"})          # no call id
        self.assertEqual(self.bus.processed, bad.seq)                       # a reconnect isn't left waiting
        self.feed.stored("pool", {"state": "open"}, turn=None)
        await asyncio.sleep(0)
        self.assertEqual(client.types[-1], "pool")
