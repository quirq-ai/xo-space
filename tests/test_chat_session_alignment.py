"""Chat and sessions work the same way on every backend.

docs/session15sept/agent-backend-parity-audit.md §3.3:

- Stop cancels the running turn, and a cancelled CLI turn takes its process
  group down with it (F-C1).
- A chat opened without a project keeps its index row, so it resumes (F-S7).
- A session is listed once even when its backend also lists its native store
  (F-S6).
- hermes and openclaw chats go through the shared dispatcher like the CLI
  backends: XO mints the session id, the index row exists before the request,
  and the next turn resumes from it (F-C3, F-C4, F-S5).
- openclaw is read from the SQLite store current releases write, and its
  roster is written as ``agents.entries`` (the ``agents.list`` form survives
  only in configs that already use it).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from routers.cowork_agent import chat as chat_router
from services.cowork_agent.adapters import process as process_mod
from services.cowork_agent.adapters.hermes import adapter as hermes_adapter
from services.cowork_agent.adapters.hermes import sessionslist as hermes_rows
from services.cowork_agent.adapters.hermes import streaming as hermes_streaming
from services.cowork_agent.adapters.openclaw import adapter as openclaw_adapter
from services.cowork_agent.adapters.openclaw import agent_db
from services.cowork_agent.adapters.openclaw import sessions as oc_sessions
from services.cowork_agent.adapters.openclaw import store as oc_store
from services.cowork_agent.adapters.openclaw import streaming as oc_streaming
from services.cowork_agent.adapters.openclaw import usage as oc_usage
from services.cowork_agent.engine import sessions_io


class _Sandbox(unittest.TestCase):
    """A throwaway state root and projects root."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        (self.base / "state").mkdir()
        (self.base / "projects").mkdir()
        env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.base / "state"),
            "XO_PROJECTS_ROOT": str(self.base / "projects"),
            "QUIRQ_RUNTIME_FILE": "",
            "QUIRQ_SECRETS_FILE": "",
        })
        env.start()
        self.addCleanup(env.stop)


# ── Sessions index ────────────────────────────────────────────────────────────


class NoProjectScopeTests(_Sandbox):
    def test_a_no_project_row_is_written_and_found_without_a_project_folder(self) -> None:
        key = "fake:default:web:aaaaaaaa"
        row = {"sessionId": "xo-1", "nativeSessionId": "n-1", "backend": "fake", "updatedAt": 1}
        self.assertTrue(sessions_io.write_session_row("default", key, row))
        self.assertEqual(sessions_io.read_session_index("default")[key]["sessionId"], "xo-1")
        scopes = [pid for pid, _dir, index in sessions_io.iter_session_indexes() if key in index]
        self.assertEqual(scopes, ["default"])
        self.assertFalse((self.base / "projects" / "default").exists())


class ListingDedupeTests(_Sandbox):
    def test_the_native_listing_does_not_repeat_an_indexed_session(self) -> None:
        sessions_io.write_session_row(
            "default", "fake:default:web:bbbbbbbb",
            {"sessionId": "xo-1", "nativeSessionId": "native-1", "backend": "fake", "updatedAt": 1},
        )
        native_row = {"id": "native-1", "time_updated": "2026-01-01T00:00:00+00:00"}
        capability = type("Cap", (), {
            "USES_PROJECT_SESSIONS": True,
            "list_native_sessions": staticmethod(lambda: [native_row]),
        })
        with patch("services.xo_manifest.resolve_agent_name", return_value="fake"), \
             patch.object(sessions_io, "_sessions_capability", return_value=capability):
            ids = [s["id"] for s in sessions_io.load_all_sessions()]
        self.assertEqual(ids, ["xo-1"])


# ── Stop ──────────────────────────────────────────────────────────────────────


class _Request:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


class AbortTests(unittest.TestCase):
    def test_abort_cancels_the_running_turn(self) -> None:
        async def scenario():
            producer = asyncio.create_task(asyncio.sleep(3600))
            chat_router._running_producers["stream-1"] = producer
            try:
                result = await chat_router.chat_abort(_Request({"stream_id": "stream-1"}))
                with self.assertRaises(asyncio.CancelledError):
                    await producer
            finally:
                chat_router._running_producers.pop("stream-1", None)
            return result

        self.assertEqual(asyncio.run(scenario()), {"ok": True})

    def test_a_stream_registers_its_turn_until_it_ends(self) -> None:
        class FakeDispatcher:
            def __init__(self, agent_name: str) -> None:
                pass

            async def stream(self, question, session_id, **kwargs):
                yield {"type": "token", "token": "hi"}
                yield {"done": True, "native_session_id": "n"}

        async def scenario():
            info = {"agent_name": "fake", "question": "q", "our_session_id": "s", "is_new_session": False}
            gen = chat_router._dispatcher_sse(info, None, "stream-2")
            first = await gen.__anext__()
            registered = "stream-2" in chat_router._running_producers
            rest = [chunk async for chunk in gen]
            return first, rest, registered, "stream-2" in chat_router._running_producers

        with patch("services.cowork_agent.engine.dispatcher.AgentDispatcher", FakeDispatcher):
            first, _rest, registered, still_registered = asyncio.run(scenario())
        self.assertIn("text-delta", first)
        self.assertTrue(registered)
        self.assertFalse(still_registered)


def _alive(pid: int) -> bool:
    """True while ``pid`` runs; a zombie or a missing process counts as gone."""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False
        if stat.rsplit(")", 1)[-1].split()[0] == "Z":
            return False
        time.sleep(0.05)
    return True


@unittest.skipUnless(hasattr(os, "killpg") and Path("/proc").is_dir(), "needs POSIX process groups and /proc")
class TerminateProcessTreeTests(unittest.TestCase):
    def test_the_whole_process_group_is_stopped(self) -> None:
        async def scenario():
            proc = await asyncio.create_subprocess_exec(
                "sh", "-c", "sleep 60 & echo $!; wait",
                stdout=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            child = int((await proc.stdout.readline()).decode().strip())
            await process_mod.terminate_process_tree(proc)
            return proc.returncode, child

        returncode, child = asyncio.run(scenario())
        self.assertIsNotNone(returncode)
        self.assertFalse(_alive(child))

    def test_a_finished_process_is_left_alone(self) -> None:
        async def scenario():
            proc = await asyncio.create_subprocess_exec("true")
            await proc.wait()
            await process_mod.terminate_process_tree(proc)
            await process_mod.terminate_process_tree(None)
            return proc.returncode

        self.assertEqual(asyncio.run(scenario()), 0)


# ── hermes ────────────────────────────────────────────────────────────────────


class HermesChatTests(_Sandbox):
    SID = "3f1c2b4a-0000-4000-8000-000000000001"

    def _turn(self, **kwargs):
        requests: list[tuple[str | None, bool]] = []

        async def fake_stream(question, session_id, *, gateway_base=None):
            requests.append((session_id, hermes_rows.find_session_row(self.SID) is not None))
            yield {"type": "token", "token": "hello"}
            yield {"done": True, "native_session_id": session_id}

        adapter = hermes_adapter.HermesAdapter.__new__(hermes_adapter.HermesAdapter)

        async def collect():
            return [event async for event in adapter.stream("hi", None, **kwargs)]

        with patch.object(hermes_streaming, "stream_to_normalized", fake_stream), \
             patch.object(hermes_adapter.HermesAdapter, "_resolve_profile", staticmethod(lambda *a: None)), \
             patch.object(hermes_adapter.HermesAdapter, "_resolve_gateway_base", staticmethod(lambda p: None)), \
             patch("services.cowork_agent.adapters.hermes.state_db.register_inflight_exchange"):
            events = asyncio.run(collect())
        return requests, events

    def test_a_new_chat_sends_the_xo_id_and_is_indexed_before_the_request(self) -> None:
        requests, events = self._turn(our_session_id=self.SID, is_new_session=True, agent_id="proj")
        self.assertEqual(requests, [(self.SID, True)])
        self.assertEqual(events[-1], {"done": True, "native_session_id": self.SID})
        self.assertNotIn("session-id-resolved", [e.get("type") for e in events])
        row = sessions_io.read_session_index("proj")["hermes:proj:web:3f1c2b4a"]
        self.assertEqual((row["sessionId"], row["nativeSessionId"], row["backend"]), (self.SID, self.SID, "hermes"))
        self.assertTrue((self.base / "projects" / "proj").is_dir())

        requests, _ = self._turn(our_session_id=self.SID, is_new_session=False)
        self.assertEqual(requests, [(self.SID, True)])

    def test_a_chat_without_a_project_keeps_its_row(self) -> None:
        self._turn(our_session_id=self.SID, is_new_session=True, agent_id=None)
        self.assertIn("hermes:default:web:3f1c2b4a", sessions_io.read_session_index("default"))


# ── openclaw ──────────────────────────────────────────────────────────────────


def _make_openclaw_db(agents_dir: Path, agent: str) -> Path:
    """The subset of openclaw's agent schema the adapter reads."""
    db = agents_dir / agent / "agent" / agent_db.AGENT_DB_NAME
    db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript("""
            CREATE TABLE session_nodes (
              session_key TEXT PRIMARY KEY, current_session_id TEXT NOT NULL,
              created_at INTEGER, updated_at INTEGER NOT NULL,
              label TEXT, display_name TEXT, archived_at INTEGER);
            CREATE TABLE session_windows (
              session_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
              created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
              transcript_updated_at INTEGER);
            CREATE TABLE transcript_events (
              session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT NOT NULL,
              created_at INTEGER NOT NULL, PRIMARY KEY (session_id, seq));
        """)
        conn.commit()
    return db


def _gateway_turn(db: Path, session_key: str, session_id: str, at: int, text: str, reply: str, usage: dict) -> None:
    """What the gateway records for one turn under ``session_key``."""
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "INSERT INTO session_nodes (session_key, current_session_id, created_at, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET current_session_id = excluded.current_session_id,"
            " updated_at = excluded.updated_at",
            (session_key, session_id, at, at),
        )
        conn.execute("INSERT OR IGNORE INTO session_windows VALUES (?, ?, ?, ?, ?)", (session_id, session_key, at, at, at))
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM transcript_events WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        events = []
        if seq == 0:
            events.append({"type": "session", "id": session_id, "timestamp": "2026-09-15T10:00:00Z", "cwd": "/"})
        events.append({"type": "message", "id": f"u{seq}", "parentId": None, "timestamp": "2026-09-15T10:00:01Z",
                       "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
        events.append({"type": "message", "id": f"a{seq}", "parentId": None, "timestamp": "2026-09-15T10:00:02Z",
                       "message": {"role": "assistant", "content": [{"type": "text", "text": reply}],
                                   "model": "m", "usage": usage}})
        for offset, event in enumerate(events):
            conn.execute("INSERT INTO transcript_events VALUES (?, ?, ?, ?)", (session_id, seq + offset, json.dumps(event), at))
        conn.commit()


USAGE = {"input": 10, "output": 5, "cacheRead": 2, "cacheWrite": 1, "cost": {"total": 0.01}}


class OpenclawRosterTests(unittest.TestCase):
    def test_entries_are_read_and_written_as_entries(self) -> None:
        cfg = {"agents": {"entries": {"main": {"name": "Main"}}}}
        self.assertEqual([e["id"] for e in oc_store.list_agent_entries(cfg)], ["main"])
        out = oc_store.apply_agent_entry(cfg, "research", "Research", Path("/w"))
        self.assertEqual(out["agents"]["entries"]["main"], {"name": "Main"})
        self.assertEqual(out["agents"]["entries"]["research"], {"name": "Research", "workspace": "/w"})
        self.assertNotIn("list", out["agents"])

    def test_a_config_that_uses_the_legacy_list_keeps_it(self) -> None:
        cfg = {"agents": {"list": [{"id": "main", "default": True}]}}
        out = oc_store.apply_agent_entry(cfg, "research", "Research", Path("/w"))
        self.assertEqual([e["id"] for e in out["agents"]["list"]], ["main", "research"])
        self.assertNotIn("entries", out["agents"])

    def test_a_config_without_a_roster_gets_entries_with_the_default_agent(self) -> None:
        out = oc_store.apply_agent_entry({}, "research", "Research", Path("/w"))
        self.assertEqual(sorted(out["agents"]["entries"]), ["main", "research"])


class OpenclawStoreTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.agents_dir = self.base / "openclaw" / "agents"
        self.db = _make_openclaw_db(self.agents_dir, "main")
        agents_dir = patch.object(agent_db, "AGENTS_DIR", self.agents_dir)
        agents_dir.start()
        self.addCleanup(agents_dir.stop)

    def test_reads_follow_the_session_key_across_generations(self) -> None:
        key = "agent:main:web:cafebabe"
        _gateway_turn(self.db, key, "gen-1", 1000, "a", "b", USAGE)
        _gateway_turn(self.db, key, "gen-2", 2000, "c", "d", USAGE)
        self.assertEqual(agent_db.session_id_for_key(key), "gen-2")
        self.assertEqual(agent_db.find_session("gen-1"), ("main", key))
        self.assertEqual(agent_db.list_generations("main", key), ["gen-1", "gen-2"])
        texts = [
            r["message"]["content"][0]["text"]
            for r in agent_db.read_conversation("main", "gen-2")
            if r.get("type") == "message"
        ]
        self.assertEqual(texts, ["a", "b", "c", "d"])
        self.assertEqual([s.session_id for s in agent_db.list_sessions("main")], ["gen-2"])
        locator = agent_db.generation_locator("main", "gen-1")
        self.assertEqual(agent_db.parse_generation_locator(locator), ("main", "gen-1"))

    def test_usage_reads_database_generations(self) -> None:
        _gateway_turn(self.db, "agent:main:web:cafebabe", "gen-1", 1000, "a", "b", USAGE)
        files = oc_usage.get_session_files(agent_id="main")
        self.assertEqual(files, [agent_db.generation_locator("main", "gen-1")])
        meta, entries = oc_usage.parse_file(files[0])
        self.assertEqual(meta["sessionId"], "gen-1")
        self.assertEqual([e["role"] for e in entries], ["user", "assistant"])

    def test_a_legacy_agent_without_a_database_is_still_read(self) -> None:
        sessions_dir = self.agents_dir / "old" / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "sessions.json").write_text(json.dumps({"agent:old:web:1": {"sessionId": "legacy-1", "updatedAt": 5}}))
        (sessions_dir / "legacy-1.jsonl").write_text(
            json.dumps({"type": "message", "timestamp": "t", "message": {"role": "user", "content": [{"type": "text", "text": "x"}]}}) + "\n"
        )
        self.assertEqual(agent_db.session_id_for_key("agent:old:web:1"), "legacy-1")
        self.assertEqual(agent_db.find_session("legacy-1"), ("old", "agent:old:web:1"))
        self.assertEqual(len(agent_db.read_conversation("old", "legacy-1")), 1)


class OpenclawChatTests(OpenclawStoreTests):
    SID = "9a8b7c6d-0000-4000-8000-000000000002"
    KEY = "agent:main:web:9a8b7c6d"

    def _turn(self, question: str, **kwargs):
        keys: list[str] = []

        async def fake_stream(q, session_key):
            keys.append(session_key)
            _gateway_turn(self.db, session_key, "oc-native-1", 1000, q, "reply", USAGE)
            yield {"type": "token", "token": "reply"}

        adapter = openclaw_adapter.OpenclawAdapter.__new__(openclaw_adapter.OpenclawAdapter)

        async def collect():
            return [event async for event in adapter.stream(question, None, **kwargs)]

        with patch.object(oc_streaming, "stream_to_normalized", fake_stream), \
             patch.object(openclaw_adapter.OpenclawAdapter, "_resolve_openclaw_agent", staticmethod(lambda a: "main")):
            events = asyncio.run(collect())
        return keys, events

    def test_a_chat_is_indexed_resumed_and_read_back(self) -> None:
        keys, events = self._turn("first", our_session_id=self.SID, is_new_session=True, agent_id="proj")
        self.assertEqual(keys, [self.KEY])
        self.assertEqual(events, [{"type": "token", "token": "reply"}, {"done": True, "native_session_id": "oc-native-1"}])
        row = sessions_io.read_session_index("proj")[self.KEY]
        self.assertEqual((row["sessionId"], row["nativeSessionId"], row["backend"]), (self.SID, "oc-native-1", "openclaw"))
        self.assertEqual(row["usage"]["input_tokens"], 10)

        keys, _ = self._turn("second", our_session_id=self.SID, is_new_session=False)
        self.assertEqual(keys, [self.KEY])
        row = sessions_io.read_session_index("proj")[self.KEY]
        self.assertEqual(row["usage"]["input_tokens"], 20)
        self.assertEqual(row["usage"]["cost"], 0.02)

        self.assertTrue(oc_sessions.owns_session(self.SID))
        self.assertEqual(oc_sessions.find_session_key("oc-native-1"), self.KEY)
        by_xo_id = oc_sessions.get_messages(self.SID)
        self.assertTrue(by_xo_id)
        self.assertEqual(len(by_xo_id), len(oc_sessions.get_messages("oc-native-1")))

    def test_the_native_listing_skips_sessions_xo_chat_indexed(self) -> None:
        self._turn("first", our_session_id=self.SID, is_new_session=True, agent_id="proj")
        _gateway_turn(self.db, "agent:main:cli:outside1", "outside-1", 5000, "hello", "hi", {})
        self.assertEqual([r["id"] for r in oc_sessions.list_native_sessions()], ["outside-1"])

    def test_an_unknown_session_ends_with_an_error_and_done(self) -> None:
        _keys, events = self._turn("x", our_session_id="missing", is_new_session=False)
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[-1], {"done": True, "native_session_id": None})
