"""Storm's stream routes (ch 17 §2.4): GET /api/chat/stream,
/api/chat/sessions/{id}/stream and /api/chat/events. Shapes, resume
(``Last-Event-ID`` wins over ``after_seq``), 404/422/503, and that no
storm path shadows another (H3).

httpx's ASGI transport buffers a whole response, so the SSE routes are
driven through the ASGI app directly, reading the body as it is sent."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

import httpx
from fastapi import FastAPI

from routers.cowork_agent.storm import storm_routers
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm import stream as storm_stream
from services.cowork_agent.storm.store import EngineLock
from tests.fakes import claude
from tests.storm_engine_kit import SID, Kit, message
from tests.storm_stream_kit import parse_sse


def app() -> FastAPI:
    application = FastAPI()
    for router in storm_routers:
        application.include_router(router)
    return application


class SseResponse:
    """One SSE request through the ASGI app: its status and headers, then
    its events as they arrive."""

    def __init__(self, application: FastAPI, path: str, headers: dict[str, str] | None = None) -> None:
        raw, _, query = path.partition("?")
        self._scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                       "scheme": "http", "path": raw, "raw_path": raw.encode(), "query_string": query.encode(),
                       "root_path": "", "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1),
                       "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
        self._app = application
        self._sent: asyncio.Queue = asyncio.Queue()
        self._gone = asyncio.Event()
        self._buffer = ""
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = b""

    async def __aenter__(self) -> "SseResponse":
        async def receive() -> dict[str, Any]:
            await self._gone.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            await self._sent.put(message)
        self._task = asyncio.create_task(self._app(self._scope, receive, send))
        start = await asyncio.wait_for(self._sent.get(), 5)
        self.status = start["status"]
        self.headers = {k.decode(): v.decode() for k, v in start["headers"]}
        return self

    async def __aexit__(self, *exc) -> None:
        self._gone.set()
        try:
            await asyncio.wait_for(self._task, 5)
        except asyncio.CancelledError:
            pass

    async def event(self) -> tuple[int | None, str, dict[str, Any]]:
        while "\n\n" not in self._buffer:
            message = await asyncio.wait_for(self._sent.get(), 5)
            chunk = message.get("body", b"")
            self.body += chunk
            self._buffer += chunk.decode()
            if not message.get("more_body", False) and "\n\n" not in self._buffer:
                raise EOFError("the stream ended")
        text, self._buffer = self._buffer.split("\n\n", 1)
        return parse_sse(text + "\n\n")

    async def until(self, kind: str) -> list[tuple[int | None, str, dict[str, Any]]]:
        seen = []
        while not seen or seen[-1][1] != kind:
            seen.append(await self.event())
        return seen


class StreamRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID),
                           *claude.streamed(SID, "m1", [("text", "Hello there.")]),
                           claude.result(SID, "Hello there."), claude.idle(SID)])
        self.app = app()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        storm_stream.detach()
        await self.kit.close()

    async def boot(self) -> tuple[storm_engine.Engine, storm_stream.Bus]:
        bus = storm_stream.listener(self.kit.clock)
        engine = await self.kit.boot(listeners=[bus])
        return engine, await storm_stream.attach(bus, engine)

    async def turn(self) -> str:
        engine, _bus = await self.boot()
        sid = await self.kit.session()
        await engine.deliver(sid, message(1))
        await self.kit.recorder.seen("session", session_id=sid, status="warm", count=2)    # the input ended
        return sid

    async def test_the_global_stream_sends_a_snapshot_then_live_frames_with_seq_ids(self) -> None:
        engine, _bus = await self.boot()
        async with SseResponse(self.app, "/api/chat/stream") as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            self.assertEqual(response.headers["cache-control"], "no-cache")
            seq, kind, wire = await response.event()
            self.assertEqual((seq, kind, wire["data"]["turns"]), (None, "snapshot", []))
            sid = await self.kit.session()
            await engine.deliver(sid, message(1))
            frames = await response.until("turn.finished")
        stored = {e.seq: e for e in self.kit.recorder.committed_events}
        for ident, kind, wire in frames:
            with self.subTest(kind=kind, id=ident):
                self.assertFalse(kind.startswith("step."))
                self.assertEqual(set(wire), {"type", "id", "seq", "ts", "chat_id", "session_id", "thread_ts",
                                             "turn_id", "run_id", "data"})
                self.assertEqual(ident, wire["id"])
                if ident is not None:
                    self.assertEqual(ident.split(":")[0], str(wire["seq"]))
                    self.assertEqual(wire["session_id"], stored[wire["seq"]].session_id)
        answer = next(w["data"]["block"] for _, k, w in frames if k == "block.finished")
        self.assertEqual((answer["type"], answer["text"]), ("answer", "Hello there."))

    async def test_resume_last_event_id_wins_over_after_seq_and_a_bad_one_is_refused(self) -> None:
        sid = await self.turn()
        started = self.kit.recorder.of("turn.started", sid)[0]
        finished = self.kit.recorder.of("turn.finished", sid)[0]
        async with SseResponse(self.app, "/api/chat/stream?after_seq=0",
                               headers={"Last-Event-ID": str(started.seq)}) as response:
            frames = await response.until("snapshot")
        replayed = sorted({w["seq"] for _, _, w in frames if w["seq"] is not None})
        self.assertEqual(replayed, [e.seq for e in self.kit.recorder.committed_events if e.seq > started.seq])
        self.assertLess(started.seq, finished.seq)
        self.assertEqual([k for _, k, _ in frames if k.startswith(("block", "turn"))],
                         ["block.started", "block.finished", "turn.finished"])

        for bad_id in ("nope", "²", "7:x", "1" * 20, "-3", "9223372036854775808"):
            with self.subTest(last_event_id=bad_id):
                bad = await self.client.get("/api/chat/stream", headers={"Last-Event-ID": bad_id.encode()})
                self.assertEqual((bad.status_code, bad.json()["detail"]["code"]), (400, "bad_last_event_id"))
        for after in (-1, 2**63, 10**30):
            with self.subTest(after_seq=after):
                response = await self.client.get("/api/chat/stream", params={"after_seq": after})
                self.assertEqual(response.status_code, 422)
                self.assertEqual((await self.client.get("/api/chat/events", params={"after_seq": after})).status_code,
                                 422)

    async def test_a_session_stream_carries_only_that_session_and_an_unknown_one_is_404(self) -> None:
        sid = await self.turn()
        other = await self.kit.session()
        async with SseResponse(self.app, f"/api/chat/sessions/{sid}/stream?after_seq=0") as response:
            frames = await response.until("snapshot")
        self.assertEqual({w["session_id"] for _, k, w in frames if k != "snapshot"}, {sid})
        self.assertNotIn(other, str(frames))
        missing = await self.client.get("/api/chat/sessions/cse_nope/stream")
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "session_not_found"))

    async def test_the_state_log_pages_by_seq_with_the_streams_shapes(self) -> None:
        sid = await self.turn()
        page = (await self.client.get("/api/chat/events", params={"session": sid, "limit": 500})).json()
        kinds = [e["type"] for e in page["events"]]
        self.assertNotIn("step.text", kinds)
        self.assertEqual([k for k in kinds if k.startswith(("block", "turn"))],
                         ["turn.started", "block.started", "block.finished", "turn.finished"])
        self.assertEqual((page["next_after_seq"], page["next_before_seq"]), (None, None))

        everything = [e.seq for e in self.kit.recorder.committed_events if e.session_id == sid]
        first = (await self.client.get("/api/chat/events", params={"session": sid, "limit": 2})).json()
        self.assertEqual(first["next_after_seq"], everything[1])
        rest = (await self.client.get("/api/chat/events", params={"session": sid, "limit": 500,
                                                                  "after_seq": first["next_after_seq"]})).json()
        # Every stored event is there: as itself, or (a step) as the blocks it gives.
        self.assertEqual({e["seq"] for e in first["events"] + rest["events"]}, set(everything))
        newest = (await self.client.get("/api/chat/events", params={"session": sid, "limit": 2,
                                                                    "before_seq": everything[-1] + 1})).json()
        self.assertEqual([e["seq"] for e in newest["events"]][-1], everything[-1])
        self.assertEqual(newest["next_before_seq"], everything[-2])
        blocks = (await self.client.get("/api/chat/events", params={"session": sid, "kinds": "block"})).json()
        self.assertEqual({e["type"] for e in blocks["events"]}, {"block.started", "block.finished"})
        self.assertEqual((await self.client.get("/api/chat/events", params={"limit": 501})).status_code, 422)

    async def test_a_stored_row_the_presenter_cant_read_never_breaks_a_replay_or_the_state_log(self) -> None:
        sid = await self.turn()
        started = await self.kit.engine.write(lambda w: w.append("turn.started", {"origin": "person"},
                                                                 session_id=sid, turn_id="turn_open"))
        with self.assertLogs("services.cowork_agent.storm.stream.turns", "ERROR"):
            bad = await self.kit.engine.write(lambda w: w.append("step.tool", {"kind": "read"}, session_id=sid,
                                                                 turn_id="turn_open"))   # no call id
            await self.kit.engine.write(lambda w: w.append("pool", {"state": "open"}, session_id=sid))
            page = await self.client.get("/api/chat/events", params={"session": sid, "limit": 500})
            async with SseResponse(self.app, f"/api/chat/stream?after_seq={started}") as response:
                frames = await response.until("snapshot")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(bad, [e["seq"] for e in page.json()["events"]])
        self.assertIn("pool", [k for _, k, _ in frames])                    # the replay went past it

    async def test_a_client_that_goes_away_leaves_no_subscriber(self) -> None:
        _engine, bus = await self.boot()
        async with SseResponse(self.app, "/api/chat/stream") as response:
            await response.event()
            self.assertEqual(bus.subscribers, 1)
        self.assertEqual(bus.subscribers, 0)

    async def test_kinds_on_the_stream_keep_only_those_events(self) -> None:
        sid = await self.turn()
        async with SseResponse(self.app, "/api/chat/stream?after_seq=0&kinds=turn") as response:
            first = await response.event()
            frames = [first, *await response.until("turn.finished")]
        self.assertEqual([k for _, k, _ in frames], ["turn.started", "turn.finished"])   # no snapshot: no blocks
        self.assertEqual({w["session_id"] for _, _, w in frames}, {sid})

    async def test_every_stream_route_answers_503_when_another_server_runs_the_engine(self) -> None:
        held = EngineLock.acquire()
        try:
            self.assertIsNone(await storm_engine.boot())
            for path in ("/api/chat/stream", "/api/chat/sessions/cse_x/stream", "/api/chat/events"):
                with self.subTest(path=path):
                    response = await self.client.get(path)
                    self.assertEqual((response.status_code, response.json()["detail"]["code"]),
                                     (503, "engine_elsewhere"))
        finally:
            held.release()

    def test_storm_stream_paths_shadow_nothing(self) -> None:
        """H3: today's per-stream route still answers its paths, storm's
        global stream answers its own, and the session stream sits after the
        session route's literal segments."""
        import server
        from fastapi.routing import iter_route_contexts
        from starlette.routing import Match
        routes = [ctx.route for ctx in iter_route_contexts(server.app.router.routes)]

        def endpoint(path: str, method: str = "GET"):
            scope = {"type": "http", "path": path, "method": method, "root_path": ""}
            return next((r.endpoint for r in routes if r.matches(scope)[0] == Match.FULL), None)
        from routers.cowork_agent import chat
        from routers.cowork_agent.storm import stream
        self.assertIs(endpoint("/api/chat/stream"), stream.global_stream)
        self.assertIs(endpoint("/api/chat/stream/abc123"), chat.chat_stream)
        self.assertIs(endpoint("/api/chat/sessions/cse_1/stream"), stream.session_stream)
        self.assertIs(endpoint("/api/chat/events"), stream.state_log)
        self.assertIs(endpoint("/api/chat/prompt", "POST"), chat.chat_prompt)
        self.assertIs(endpoint("/api/chat/abort", "POST"), chat.chat_abort)
        self.assertIs(endpoint("/api/chat/active"), chat.chat_active)
        self.assertIsNone(endpoint("/api/chat/respond", "POST"))
        paths = server.app.openapi()["paths"]
        for path in ("/api/chat/stream", "/api/chat/sessions/{session_id}/stream", "/api/chat/events"):
            self.assertEqual(set(paths[path]), {"get"})


if __name__ == "__main__":
    unittest.main()
