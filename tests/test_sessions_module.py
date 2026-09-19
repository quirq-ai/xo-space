"""The sessions module: what ``modules/sessions/`` declares and answers.

``tests/test_modules.py`` holds every module to the general contract; this
file pins the sessions-specific facts:

* the manifest declares api, stream, commands and the List page, no folder
  of its own, and the three legacy prefixes it keeps serving as aliases;
  the two lifecycle types are its, and the timeline accepts them;
* ``start()`` runs the dispatcher's stream for a purpose, stamps the purpose
  on the session's index row next to the adapter's fields (updating the
  adapter's row when it wrote one, writing a minimal row when none did),
  emits ``session.started`` once to the project's timeline and raises the
  ``sessions.started`` signal once; a bad purpose or an empty prompt is
  refused before any stream runs;
* ``record_purpose`` keeps a purpose it is told not to overwrite; the
  listing carries ``backend`` and ``purpose`` beside the shape it always had;
* the routes answer the same paths and bodies ``routers/cowork_agent/
  sessions.py`` and ``chat.py`` did, the old import paths resolve to the
  moved objects, and a chat stream stamps ``purpose: "chat"``;
* the stream follows the lifecycle lines of the project timelines and
  refuses a type outside them; the List page validates and every field it
  names is a key of the listing's rows; the commands answer through the facade.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import modules.timeline.service as timeline_service
from modules.sessions import commands, events, routes, service, session_transcript, sessions_io, store, stream
from services import modules as registry
from services import signals
from services.errors import ServiceError
from services.timestamps import parse_ts
from tests.support import ROOT, SAMPLE_PROJECT, Sandbox, client, collect, fake_stream

PID = "00000000-0000-4000-8000-000000000000"
FIXTURE_SESSION = "sample-session-1"
FIXTURE_KEY = "agent:sample_agent:11111111-1111-4111-8111-111111111111"
PAGE = ROOT / "modules" / "sessions" / "pages" / "list.json"
PAGE_SCHEMA = ROOT / "services" / "schema" / "page.schema.json"
AGENT = "sample_agent"


class _FakeDispatcher:
    """What ``service._dispatcher`` and ``AgentDispatcher`` stand in for:
    ``stream`` yields the dispatcher's shape (``tests.support.fake_stream``)."""

    def __init__(self, agent_name: str = AGENT, tokens=("hel", "lo"), native: str = "native-1") -> None:
        self.agent_name = agent_name
        self.stream = fake_stream(list(tokens), native_session_id=native)


class _SessionsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox(self)
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)
        timeline_service.reset_for_tests()
        self.addCleanup(timeline_service.reset_for_tests)

    def timeline(self) -> list[dict]:
        path = self.sandbox.state / "projects" / PID / "timeline.jsonl"
        return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]

    def started_lines(self, session_id: str) -> list[dict]:
        return [line for line in self.timeline()
                if line["type"] == "session.started" and line.get("session_id") == session_id]

    def index(self) -> dict:
        return sessions_io.read_session_index(SAMPLE_PROJECT)

    def rows_for(self, session_id: str) -> dict:
        return {key: row for key, row in self.index().items() if row.get("sessionId") == session_id}


def _fake_capability(**attrs):
    """A stand-in for an adapter's ``sessions`` capability module."""
    return types.SimpleNamespace(**attrs)


class ManifestTests(_SessionsCase):
    def test_the_manifest_declares_what_the_folder_implements(self) -> None:
        module = registry.get("sessions")
        self.assertIsNone(module.folder, "the module writes into the shared projects/ tier only")
        for kind in ("api", "stream", "commands", "pages"):
            self.assertTrue(module.declares(kind), kind)
            self.assertTrue(registry.implements("sessions", kind), kind)
        for kind in ("tasks", "listeners"):
            self.assertFalse(module.declares(kind), kind)
        self.assertEqual(set(module.aliases), {"/api/sessions", "/api/messages", "/api/chat"})
        self.assertEqual([page.id for page in registry.pages("sessions")], ["list"])
        self.assertIn(("sessions", "events"), [(m.name, n) for m, n, _ in registry.streams()])
        self.assertEqual(sorted(registry.commands()["sessions"]), ["get", "list"])

    def test_every_route_sits_under_the_aliases(self) -> None:
        module = registry.get("sessions")
        allowed = ("/api/sessions",) + module.aliases
        for route in routes.router.routes:
            with self.subTest(path=route.path):
                self.assertTrue(any(route.path == a or route.path.startswith(a + "/") for a in allowed))

    def test_the_routes_keep_every_session_and_chat_path(self) -> None:
        served = sorted((route.path, method) for route in routes.router.routes for method in route.methods)
        self.assertEqual(served, [
            ("/api/chat/abort", "POST"), ("/api/chat/prompt", "POST"), ("/api/chat/respond", "POST"),
            ("/api/chat/stream/{stream_id}", "GET"),
            ("/api/messages/{session_id}", "GET"),
            ("/api/sessions", "GET"), ("/api/sessions", "POST"), ("/api/sessions/search", "GET"),
            ("/api/sessions/{session_id}", "DELETE"), ("/api/sessions/{session_id}", "GET"),
            ("/api/sessions/{session_id}", "PATCH"),
            ("/api/sessions/{session_id}/files", "GET"), ("/api/sessions/{session_id}/todos", "GET"),
            ("/api/sessions/{session_id}/transcript", "GET"),
        ])
        # /search registers before /{session_id}, so the literal path wins.
        paths = [route.path for route in routes.router.routes]
        self.assertLess(paths.index("/api/sessions/search"), paths.index("/api/sessions/{session_id}"))

    def test_the_lifecycle_types_are_this_modules_and_the_timeline_accepts_them(self) -> None:
        self.assertEqual(events.TYPES, ("session.started", "session.closed"))
        self.assertEqual(events.SIGNALS, ("started", "closed"))
        declared = registry.event_types()
        for kind in events.TYPES:
            self.assertEqual(declared.get(kind), "sessions", kind)
            self.assertIn(kind, timeline_service.declared_types())
        self.assertEqual(registry.signals_declared() & {"sessions.started", "sessions.closed"},
                         {"sessions.started", "sessions.closed"})

    def test_the_file_table_names_the_index_shard(self) -> None:
        [spec] = store.FILES
        self.assertEqual(spec.pattern, "projects/<pid>/sessions/sessionslist.d/<shard>.json")
        self.assertEqual(spec.role, "record")
        self.assertTrue(spec.matches(f"projects/{PID}/sessions/sessionslist.d/c769df1f8af960c8.json"))
        self.assertFalse(spec.matches(f"projects/{PID}/sessions/sessions-augment.json"), "the augment file is telemetry's")
        self.assertEqual([f.pattern for m, f in registry.files() if m == "sessions"], [spec.pattern])
        self.assertEqual(store.shard_path(SAMPLE_PROJECT, FIXTURE_KEY),
                         self.sandbox.state / "projects" / PID / "sessions" / "sessionslist.d" / "c769df1f8af960c8.json")
        self.assertIsNone(store.shard_path("ghost", FIXTURE_KEY))


class AliasTests(unittest.TestCase):
    def test_the_old_import_paths_resolve_to_the_moved_modules(self) -> None:
        import services.cowork_agent.session_transcript as old_transcript
        from services.cowork_agent.engine import sessions_io as old_io

        self.assertIs(old_io, sessions_io)
        self.assertIs(old_transcript, session_transcript)
        from routers.cowork_agent import chat as old_chat, sessions as old_sessions

        self.assertIs(old_chat.router, routes.router)
        self.assertIs(old_sessions.router, routes.router)


class StartTests(_SessionsCase):
    def setUp(self) -> None:
        super().setUp()
        self.dispatchers: list[_FakeDispatcher] = []
        self.signals: list[dict] = []

        def build(agent_name: str) -> _FakeDispatcher:
            self.dispatchers.append(_FakeDispatcher(agent_name))
            return self.dispatchers[-1]

        async def on_started(**payload) -> None:
            self.signals.append(payload)

        signals.on("sessions.started", on_started)
        self.addCleanup(signals.off, "sessions.started", on_started)
        for target, value in (("_dispatcher", build), ("resolve_agent_name", lambda: AGENT)):
            patcher = patch.object(service, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_new_session_gets_a_row_with_its_purpose_and_one_started_line(self) -> None:
        events_out = collect(service.start("hi", purpose="job", project_id=SAMPLE_PROJECT))
        self.assertEqual([e.get("token") for e in events_out[:-1]], ["hel", "lo"])
        self.assertEqual(events_out[-1], {"done": True, "native_session_id": "native-1"})
        self.assertEqual([d.agent_name for d in self.dispatchers], [AGENT])

        [payload] = self.signals
        sid = payload["session_id"]
        self.assertEqual(payload, {"session_id": sid, "purpose": "job", "project_id": SAMPLE_PROJECT, "runtime": AGENT})

        [line] = self.started_lines(sid)
        self.assertEqual(list(line)[:5], ["ts", "type", "pid", "project_id", "session_id"])
        self.assertEqual((line["pid"], line["project_id"], line["runtime"], line["purpose"]),
                         (PID, SAMPLE_PROJECT, AGENT, "job"))
        self.assertIsNotNone(parse_ts(line["ts"]))

        [(key, row)] = self.rows_for(sid).items()
        self.assertEqual(key, f"sessions:{AGENT}:{sid}")
        self.assertEqual((row["sessionId"], row["backend"], row["purpose"]), (sid, AGENT, "job"))
        self.assertEqual(row["directory"], str(self.sandbox.project))
        self.assertIn(FIXTURE_KEY, self.index(), "the adapter's other rows are untouched")

    def test_a_resumed_session_keeps_the_adapters_row_and_gains_the_purpose(self) -> None:
        before = self.index()[FIXTURE_KEY]
        self.assertNotIn("purpose", before)
        collect(service.start("again", purpose="inbox_item", session_id=FIXTURE_SESSION, project_id=SAMPLE_PROJECT))
        self.assertEqual([d.agent_name for d in self.dispatchers], [AGENT], "the row's own backend continues it")
        rows = self.rows_for(FIXTURE_SESSION)
        self.assertEqual(list(rows), [FIXTURE_KEY], "one row per session: nothing was written under a second key")
        after = rows[FIXTURE_KEY]
        self.assertEqual(after["purpose"], "inbox_item")
        self.assertEqual({k: v for k, v in after.items() if k != "purpose"}, before, "the adapter's fields survive")
        # Once per start(): the fixture's own line for the native id is a different session id.
        self.assertEqual(len(self.started_lines(FIXTURE_SESSION)), 1)
        self.assertEqual(len(self.signals), 1)

    def test_the_purpose_lands_on_the_row_the_adapter_writes_during_the_stream(self) -> None:
        written: list[str] = []
        seen_purpose: list = []

        async def adapter_stream(question, session_id=None, **kwargs):
            """An adapter that writes its own row before its first event, the
            way the project-tied adapters write their preliminary entry."""
            sid = kwargs["our_session_id"]
            key = f"agent:{AGENT}:{sid}"
            sessions_io.write_session_row(SAMPLE_PROJECT, key, {
                "sessionId": sid, "nativeSessionId": "n-1", "directory": "/work",
                "backend": AGENT, "updatedAt": 1, "usage": {"input_tokens": 1},
            })
            written.append(key)
            seen_purpose.append(sessions_io.read_session_index(SAMPLE_PROJECT)[key].get("purpose"))
            yield {"type": "token", "token": "a"}
            seen_purpose.append(sessions_io.read_session_index(SAMPLE_PROJECT)[key].get("purpose"))
            yield {"done": True, "native_session_id": "n-1"}

        adapter_like = types.SimpleNamespace(agent_name=AGENT, stream=adapter_stream)
        with patch.object(service, "_dispatcher", lambda name: adapter_like):
            out = collect(service.start("go", purpose="workitem", project_id=SAMPLE_PROJECT))
        self.assertEqual(seen_purpose, [None, "workitem"], "stamped after the first event, not before the row exists")
        self.assertTrue(out[-1]["done"])
        [key] = written
        sid = key.rsplit(":", 1)[1]
        rows = self.rows_for(sid)
        self.assertEqual(list(rows), [key], "no row of the module's own beside the adapter's")
        self.assertEqual(rows[key]["purpose"], "workitem")
        self.assertEqual(rows[key]["usage"], {"input_tokens": 1})

    def test_without_a_project_the_line_goes_to_the_space_log_and_no_row_is_written(self) -> None:
        collect(service.start("hi", purpose="chat"))
        [payload] = self.signals
        sid = payload["session_id"]
        self.assertEqual(self.started_lines(sid), [])
        space = self.sandbox.state / "projects" / "timeline.jsonl"
        lines = [json.loads(raw) for raw in space.read_text(encoding="utf-8").splitlines() if raw.strip()]
        [line] = [l for l in lines if l.get("session_id") == sid]
        self.assertEqual((line["type"], line["purpose"]), ("session.started", "chat"))
        self.assertNotIn("pid", line)
        self.assertIsNone(sessions_io.find_session_row(sid))

    def test_a_bad_purpose_or_an_empty_prompt_is_refused_before_any_stream(self) -> None:
        for purpose in ("Not Valid", "", None, "x" * 40):
            with self.subTest(purpose=purpose), self.assertRaises(ServiceError) as caught:
                service.start("hi", purpose=purpose, project_id=SAMPLE_PROJECT)
            self.assertEqual(caught.exception.code, "invalid_purpose")
        with self.assertRaises(ServiceError) as caught:
            service.start("   ", purpose="job", project_id=SAMPLE_PROJECT)
        self.assertEqual(caught.exception.code, "empty_prompt")
        self.assertEqual(self.dispatchers, [])
        self.assertEqual(self.signals, [])
        self.assertEqual(len([l for l in self.timeline() if l["type"] == "session.started"]), 1, "only the fixture's line")


class RecordPurposeTests(_SessionsCase):
    def test_record_purpose_stamps_updates_and_keeps(self) -> None:
        self.assertTrue(service.record_purpose(FIXTURE_SESSION, "job"))
        self.assertEqual(self.index()[FIXTURE_KEY]["purpose"], "job")
        self.assertTrue(service.record_purpose(FIXTURE_SESSION, "chat", overwrite=False))
        self.assertEqual(self.index()[FIXTURE_KEY]["purpose"], "job", "a purpose is kept when told not to overwrite")
        self.assertTrue(service.record_purpose(FIXTURE_SESSION, "chat"))
        self.assertEqual(self.index()[FIXTURE_KEY]["purpose"], "chat")
        self.assertEqual(self.index()[FIXTURE_KEY]["nativeSessionId"], "11111111-1111-4111-8111-111111111111")

    def test_a_missing_row_is_not_created_unless_asked(self) -> None:
        self.assertFalse(service.record_purpose("ghost", "chat"))
        self.assertFalse(service.record_purpose("ghost", "chat", create=True), "no project to file it under")
        self.assertIsNone(sessions_io.find_session_row("ghost"))
        self.assertTrue(service.record_purpose("ghost", "chat", create=True, project_id=SAMPLE_PROJECT, backend=AGENT))
        project, key, row = sessions_io.find_session_row("ghost")
        self.assertEqual((project, key, row["purpose"], row["backend"]), (SAMPLE_PROJECT, f"sessions:{AGENT}:ghost", "chat", AGENT))
        self.assertFalse(service.record_purpose("ghost-2", "chat", create=True, project_id="no-such-project"))
        self.assertIsNone(sessions_io.find_session_row("ghost-2"))

    def test_update_session_row_merges_fields_into_the_owning_shard(self) -> None:
        self.assertTrue(sessions_io.update_session_row(FIXTURE_SESSION, purpose="job", note="x"))
        row = self.index()[FIXTURE_KEY]
        self.assertEqual((row["purpose"], row["note"], row["backend"]), ("job", "x", AGENT))
        self.assertTrue(sessions_io.update_session_row(FIXTURE_SESSION, purpose="job"), "already there: no rewrite")
        self.assertFalse(sessions_io.update_session_row("ghost-9", purpose="job"))
        self.assertEqual(sessions_io.find_session_row(FIXTURE_SESSION)[:2], (SAMPLE_PROJECT, FIXTURE_KEY))

    def test_the_partition_with_the_watcher_augment_accepts_purpose(self) -> None:
        from services.storage import reader

        service.record_purpose(FIXTURE_SESSION, "job")
        augment = json.loads((self.sandbox.state / "projects" / PID / "sessions" / "sessions-augment.json").read_text())
        merged = reader.merge_sessionslist(self.index(), augment)
        self.assertEqual(merged[FIXTURE_KEY]["purpose"], "job")
        # The watcher's row for the same session: the two field sets stay disjoint.
        [augment_row] = augment["sessions"].values()
        stitched = reader.merge_session_record(self.index()[FIXTURE_KEY], augment_row)
        self.assertEqual((stitched["purpose"], stitched["messageCount"]), ("job", 4))
        self.assertNotIn("purpose", reader.augment_field_names())


class ListingTests(_SessionsCase):
    """``load_all_sessions`` over the sample, with the active backend's
    sessions capability standing in (no adapter is named)."""

    def setUp(self) -> None:
        super().setUp()
        import services.xo_manifest as manifest

        for target, value in ((sessions_io, "_sessions_capability"), (manifest, "resolve_agent_name")):
            patcher = patch.object(target, value, (lambda *_a, **_k: _fake_capability(USES_PROJECT_SESSIONS=True))
                                   if value == "_sessions_capability" else (lambda: AGENT))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_listing_carries_backend_and_purpose_beside_its_shape(self) -> None:
        [row] = service.list_sessions()
        self.assertEqual((row["id"], row["backend"], row["purpose"]), (FIXTURE_SESSION, AGENT, None))
        for key in ("title", "directory", "time_updated", "time_created", "agent", "summary_diffs"):
            self.assertIn(key, row)
        service.record_purpose(FIXTURE_SESSION, "chat")
        [row] = service.list_sessions()
        self.assertEqual(row["purpose"], "chat")
        self.assertEqual(service.get(FIXTURE_SESSION)["purpose"], "chat")
        self.assertEqual(service.search("untitled"), [{"session": row, "snippet": None}])
        self.assertEqual(service.search("nothing like it"), [])
        with self.assertRaises(service.SessionNotFound) as caught:
            service.get("nope")
        self.assertEqual((caught.exception.status, caught.exception.detail), (404, "Session not found"))

    def test_the_page_names_only_fields_the_listing_answers(self) -> None:
        spec = json.loads(PAGE.read_text(encoding="utf-8"))
        [row] = service.list_sessions()
        [block] = [b for b in spec["blocks"] if b["type"] == "list"]
        named = [block["key"], block["row"]["title"], block["row"]["detail"], block["row"]["badge"]["value"]]
        named += [expr.split("|")[0] for expr in block["row"]["meta"]]
        for field in named:
            with self.subTest(field=field):
                self.assertIn(field, row)


SID = "f2d46667-4ac1-4e03-973e-c9f86832250d"
LISTING = [{"id": SID, "title": "Summarize what this project is", "backend": AGENT, "purpose": None}]


class RouteTests(_SessionsCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = client(routes.router)

    def test_the_read_routes(self) -> None:
        with patch.object(service, "load_all_sessions", return_value=LISTING):
            self.assertEqual(self.client.get("/api/sessions").json(), LISTING)
            self.assertEqual(self.client.get("/api/sessions?limit=0").json(), [])
            self.assertEqual(self.client.get("/api/sessions/search?q=SUMMARIZE").json(),
                             [{"session": LISTING[0], "snippet": None}])
            self.assertEqual(self.client.get("/api/sessions/search?q=zzz").json(), [])
            self.assertEqual(self.client.get(f"/api/sessions/{SID}").json(), LISTING[0])
            missing = self.client.get("/api/sessions/nope")
        self.assertEqual((missing.status_code, missing.json()), (404, {"detail": "Session not found"}))
        self.assertEqual(self.client.get(f"/api/sessions/{SID}/todos").json(), {"todos": []})
        self.assertEqual(self.client.get(f"/api/sessions/{SID}/files").json(), {"files": []})

    def test_messages_read_through_the_owning_adapter(self) -> None:
        record = [{"id": f"m{i}", "data": {"role": "user"}, "parts": []} for i in range(3)]
        capability = _fake_capability(get_messages=lambda sid: record)
        with patch.object(service, "find_session_backend", return_value="x"), \
             patch.object(service, "try_load_capability", return_value=capability):
            latest = self.client.get(f"/api/messages/{SID}").json()
            page = self.client.get(f"/api/messages/{SID}?limit=2&offset=1").json()
        self.assertEqual(latest, {"total": 3, "offset": 0, "messages": record})
        self.assertEqual(page, {"total": 3, "offset": 1, "messages": record[1:3]})
        with patch.object(service, "find_session_backend", return_value=None):
            self.assertEqual(self.client.get(f"/api/messages/{SID}").json(), {"total": 0, "offset": 0, "messages": []})

    def test_the_transcript_route(self) -> None:
        record = [{"id": "u1", "session_id": SID, "time_created": "t", "data": {"role": "user"},
                   "parts": [{"data": {"type": "text", "text": "hello"}}]}]
        capability = _fake_capability(get_messages=lambda sid: record)
        with patch.object(session_transcript, "load_all_sessions", return_value=LISTING), \
             patch.object(session_transcript, "find_session_backend", return_value="x"), \
             patch.object(session_transcript, "try_load_capability", return_value=capability):
            body = self.client.get(f"/api/sessions/{SID}/transcript").json()
        self.assertEqual(body, {"title": "Summarize what this project is",
                                "messages": [{"id": "u1", "role": "user", "content": "hello"}]})
        with patch.object(session_transcript, "load_all_sessions", return_value=[]):
            missing = self.client.get("/api/sessions/nope/transcript")
        self.assertEqual((missing.status_code, missing.json()), (404, {"detail": "Session not found"}))

    def test_the_write_routes(self) -> None:
        created = self.client.post("/api/sessions").json()
        self.assertEqual(created["title"], "New Chat")
        self.assertEqual(len(created["id"]), 36)
        self.assertEqual(self.client.delete(f"/api/sessions/{SID}").json(), {"ok": True})

        self.assertEqual(self.client.patch(f"/api/sessions/{SID}", json={}).json(), {"ok": True})
        self.assertEqual(self.client.patch(f"/api/sessions/{SID}", content=b"not json").json(), {"ok": True})
        bad = self.client.patch(f"/api/sessions/{SID}", json={"directory": "  "})
        self.assertEqual((bad.status_code, bad.json()), (400, {"detail": "directory must be a non-empty string"}))
        with patch.object(service, "list_adapters", return_value=[]):
            missing = self.client.patch(f"/api/sessions/{SID}", json={"directory": "/work"})
        self.assertEqual((missing.status_code, missing.json()), (404, {"detail": "Session not found"}))
        owner = _fake_capability(set_session_directory=lambda sid, d: {"ok": True, "session_id": sid, "directory": d})
        with patch.object(service, "list_adapters", return_value=["x"]), \
             patch.object(service, "try_load_capability", return_value=owner):
            done = self.client.patch(f"/api/sessions/{SID}", json={"directory": "/work"}).json()
        self.assertEqual(done, {"ok": True, "session_id": SID, "directory": "/work"})

    def test_the_chat_routes(self) -> None:
        empty = self.client.post("/api/chat/prompt", json={"text": "  "})
        self.assertEqual((empty.status_code, empty.json()), (400, {"detail": "Empty message"}))
        self.assertEqual(self.client.post("/api/chat/abort", json={"stream_id": "nope"}).json(), {"ok": True})
        self.assertEqual(self.client.post("/api/chat/respond", json={}).json(), {"ok": True})
        unknown = self.client.get("/api/chat/stream/nope")
        self.assertEqual(unknown.headers["content-type"].split(";")[0], "text/event-stream")
        self.assertIn("Stream not found", unknown.text)

    def test_a_chat_stream_stamps_purpose_chat_on_the_row_it_drives(self) -> None:
        self.assertNotIn("purpose", self.index()[FIXTURE_KEY])
        with patch.object(routes, "_resolve_user_id", AsyncMock(return_value=None)), \
             patch("services.cowork_agent.engine.dispatcher.AgentDispatcher", _FakeDispatcher):
            registered = self.client.post("/api/chat/prompt", json={
                "text": "hi", "session_id": FIXTURE_SESSION, "agent_name": AGENT}).json()
            self.assertEqual(registered["session_id"], FIXTURE_SESSION)
            played = self.client.get(f"/api/chat/stream/{registered['stream_id']}")
        self.assertIn("event: text-delta", played.text)
        self.assertIn("event: done", played.text)
        self.assertIn(FIXTURE_SESSION, played.text)
        self.assertEqual(self.index()[FIXTURE_KEY]["purpose"], "chat")
        # A purpose another starter recorded is kept by the chat.
        service.record_purpose(FIXTURE_SESSION, "job")
        with patch.object(routes, "_resolve_user_id", AsyncMock(return_value=None)), \
             patch("services.cowork_agent.engine.dispatcher.AgentDispatcher", _FakeDispatcher):
            registered = self.client.post("/api/chat/prompt", json={
                "text": "more", "session_id": FIXTURE_SESSION, "agent_name": AGENT}).json()
            self.client.get(f"/api/chat/stream/{registered['stream_id']}")
        self.assertEqual(self.index()[FIXTURE_KEY]["purpose"], "job")


def _drain(gen, n: int, timeout: float = 3.0) -> list[dict]:
    """The first ``n`` lines of an async generator, then close it."""

    async def run() -> list[dict]:
        out = []
        try:
            for _ in range(n):
                out.append(await asyncio.wait_for(gen.__anext__(), timeout=timeout))
        finally:
            await gen.aclose()
        return out

    return asyncio.run(run())


class StreamTests(_SessionsCase):
    def test_the_stream_follows_the_lifecycle_lines_of_every_project(self) -> None:
        with patch.object(service, "_dispatcher", lambda name: _FakeDispatcher(name)), \
             patch.object(service, "resolve_agent_name", lambda: AGENT):
            collect(service.start("hi", purpose="job", project_id=SAMPLE_PROJECT))
        [mine] = [l for l in self.timeline() if l["type"] == "session.started" and l.get("purpose") == "job"]
        [line] = _drain(stream.follow(since="2026-06-01T00:00:00Z"), 1)
        self.assertEqual((line["project"], line["type"], line["session_id"], line["purpose"]),
                         (PID, "session.started", mine["session_id"], "job"))
        # The backlog is only lifecycle lines: the fixture's file.edited and todo.added never
        # show; the fixture's session.started is there from the project log and, as the copy
        # an older install made, from the Space log.
        backlog = _drain(stream.follow(since="2025-12-31T00:00:00Z"), 3)
        self.assertEqual([l["type"] for l in backlog], ["session.started"] * 3)
        self.assertEqual({l["project"] for l in backlog}, {PID, "space"})
        self.assertIs(stream.STREAMS["events"], stream.follow)

    def test_the_stream_narrows_but_never_widens(self) -> None:
        self.assertEqual(stream.check_types(None), frozenset(events.TYPES))
        self.assertEqual(stream.check_types([]), frozenset(events.TYPES))
        self.assertEqual(stream.check_types(["session.closed"]), frozenset({"session.closed"}))
        with self.assertRaises(ServiceError) as caught:
            stream.check_types(["todo.added"])
        self.assertEqual(caught.exception.code, "invalid_value")
        with self.assertRaises(ServiceError):
            stream.follow(types=["file.edited"])


class PageTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_the_list_page_validates(self) -> None:
        import jsonschema

        schema = json.loads(PAGE_SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft7Validator(schema).validate(json.loads(PAGE.read_text(encoding="utf-8")))

    def test_the_list_page_reads_the_listing_and_expands_the_transcript(self) -> None:
        spec = json.loads(PAGE.read_text(encoding="utf-8"))
        self.assertEqual((spec["id"], spec["tab"], spec["route"], spec["label"]), ("list", "agents", "agents/sessions-list", "Sessions"))
        self.assertEqual(spec["read"], "/api/sessions?limit=50")
        [block] = [b for b in spec["blocks"] if b["type"] == "list"]
        self.assertEqual(block["expand"]["read"], "/api/sessions/{id}/transcript")
        self.assertEqual(block["expand"]["items"], "messages")
        served = {route.path for route in routes.router.routes}
        self.assertIn("/api/sessions", served)
        self.assertIn("/api/sessions/{session_id}/transcript", served)
        tabs = {t["id"] for t in registry.tabs()}
        self.assertIn(spec["tab"], tabs)


class CommandTests(_SessionsCase):
    def test_list_and_get_answer_through_the_facade(self) -> None:
        with patch.object(service, "load_all_sessions", return_value=LISTING):
            self.assertEqual(commands.COMMANDS["list"]([]), {"sessions": LISTING})
            self.assertEqual(commands.COMMANDS["list"](["0"]), {"sessions": []})
            self.assertEqual(commands.COMMANDS["get"]([SID]), LISTING[0])
            with self.assertRaises(service.SessionNotFound):
                commands.COMMANDS["get"](["nope"])
        with self.assertRaises(ServiceError) as caught:
            commands.COMMANDS["get"]([])
        self.assertEqual(caught.exception.code, "missing_session_id")


if __name__ == "__main__":
    unittest.main()
