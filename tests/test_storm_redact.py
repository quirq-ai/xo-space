"""Redaction of what storm stores (design ch 16 §3; §0.76) and the raw
per-turn CLI logs (ch 08 §4; §0.106).

Every secret here is made up, and every store is a temp file: a value in the
secrets store, an auth variable a runtime names, each token shape and a
comms token must come out ``[redacted]`` in each kind of record storm keeps
(events, notices, errors, messages, hops, threads, review items, run titles
and summaries, the raw turn files, the logs), while ordinary text stays as
it was.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import services.decision_models as dm
from services import project_management
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import session as claude_session
from services.cowork_agent.registry import agent_env
from services.cowork_agent.storm import redact
from services.cowork_agent.storm.chats import router, rows
from services.cowork_agent.storm.comms import owner
from services.cowork_agent.storm.engine import raw_turns, records
from services.cowork_agent.storm.engine.model import SESSION_TOKEN_ENV, Wake
from services.cowork_agent.storm.engine.ports import Writes
from services.cowork_agent.storm.engine.raw_turns import RawTurnLog
from services.cowork_agent.storm.engine.records import Ids
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.review.queue import ReviewQueue
from services.cowork_agent.storm.runs import records as run_records
from services.cowork_agent.storm.store import MAX_REDELIVERIES, Store, hand_back
from services.storage.layout import turn_logs_dir
from services.timestamps import now_iso
from tests.fakes import claude
from tests.storm_comms_kit import CommsTestCase
from tests.storm_engine_kit import SID, Kit, message
from tests.test_storm_chats_flows import PC, TH, FlowTestCase, done, next_input, start

R = redact.REDACTED
STORE_KEY, STORE_VALUE = "MADE_UP_STORE_KEY", "made-up-store-value-7f3a9c"
AUTH_NAME, AUTH_VALUE = "XO_TEST_MADE_UP_AUTH", "made-up-auth-value-51be02"
COMMS = "made-up-comms-token-" + "q" * 24
SHAPES = ("sk-ant-api03-" + "Ab1" * 10, "sk-proj-" + "Cd2" * 10, "ghp_" + "Ef3" * 12, "gho_" + "Ef4" * 12,
          "ghs_" + "Ef5" * 12, "github_pat_" + "Gh6_" * 8)
BEARER = "Bearer " + "Ij7." * 8
PLAIN = "plain words stay"


def leaky() -> str:
    """One text with every kind of secret, between ordinary words."""
    return " | ".join([PLAIN, STORE_VALUE, AUTH_VALUE, COMMS, *SHAPES, BEARER, "the end"])


CLEAN = " | ".join([PLAIN, *[R] * (3 + len(SHAPES)), f"Bearer {R}", "the end"])


class SecretsCase(unittest.TestCase):
    """A secrets store of the test's own, one watched auth variable and one
    live comms token; all put back afterwards."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store_file = self.root / "secrets.env"
        self.store_file.write_text(f"# made up\n{STORE_KEY}={STORE_VALUE}\nSHORT_SETTING=true\n")
        for patch in (mock.patch.object(agent_env, "ENV_FILE", self.store_file),
                      mock.patch.dict(os.environ, {AUTH_NAME: AUTH_VALUE, "QUIRQ_STATE_ROOT": str(self.root)})):
            patch.start()
            self.addCleanup(patch.stop)
        redact.watch_env([AUTH_NAME])
        self.addCleanup(redact.watch_env, ())
        redact.add_token(COMMS)
        self.addCleanup(redact.drop_token, COMMS)


class RedactTests(SecretsCase):
    def test_every_kind_of_secret_is_redacted_and_ordinary_text_is_not(self) -> None:
        self.assertEqual(redact.redact(leaky()), CLEAN)
        for text in (PLAIN, "true", "a task-" + "x" * 30, "Bearer token", "Bearer ${XO_COMMS_TOKEN}", "sk-short",
                     "see https://example.com/path?q=1", ""):
            with self.subTest(text=text):
                self.assertIs(redact.redact(text), text)

    def test_a_short_store_value_is_not_matched(self) -> None:
        self.assertEqual(redact.redact("it is true"), "it is true")     # SHORT_SETTING=true

    def test_a_value_is_matched_as_json_writes_it_too(self) -> None:
        tricky = 'made-up"quoted\\value-123'
        self.store_file.write_text(f"QUOTED={tricky}\n")
        line = json.dumps({"text": f"x {tricky} y"})
        self.assertEqual(json.loads(redact.redact(line))["text"], f"x {R} y")

    def test_the_longer_of_two_overlapping_secrets_goes_whole(self) -> None:
        self.store_file.write_text(f"A={STORE_VALUE}\nB={STORE_VALUE}-and-more\n")
        self.assertEqual(redact.redact(f"[{STORE_VALUE}-and-more]"), f"[{R}]")

    def test_a_value_added_to_the_store_mid_run_is_caught_on_the_next_call(self) -> None:
        later = "made-up-added-later-0042"
        self.assertEqual(redact.redact(later), later)
        self.store_file.write_text(f"{STORE_KEY}={STORE_VALUE}\nLATER={later}\n")
        self.assertEqual(redact.redact(later), R)
        os.environ[AUTH_NAME] = "made-up-new-auth-value-77"           # an auth variable that changed
        self.assertEqual(redact.redact("made-up-new-auth-value-77"), R)
        self.assertEqual(redact.redact(AUTH_VALUE), AUTH_VALUE)        # the old value is no longer a secret

    def test_the_store_is_read_once_per_change_not_once_per_call(self) -> None:
        with mock.patch.object(agent_env, "load_env_entries", wraps=agent_env.load_env_entries) as read:
            self.store_file.write_text(f"{STORE_KEY}={STORE_VALUE}\nOTHER=made-up-other-value-9\n")
            for _ in range(200):
                redact.redact(leaky())
                redact.redact_data({"a": [leaky(), {"b": leaky()}]})
            self.assertEqual(read.call_count, 1)
            self.store_file.write_text(f"{STORE_KEY}={STORE_VALUE}\n")
            redact.redact(leaky())
            self.assertEqual(read.call_count, 2)

    def test_a_dropped_token_is_no_longer_matched(self) -> None:
        redact.drop_token(COMMS)
        self.assertEqual(redact.redact(COMMS), COMMS)

    def test_data_keeps_its_shape_and_is_the_same_object_when_clean(self) -> None:
        clean = {"a": [1, "x", {"b": None}], "c": ("y",)}
        self.assertIs(redact.redact_data(clean), clean)
        dirty = {"a": [1, leaky(), {"b": None}], "c": (leaky(),), "keep": 2.5}
        self.assertEqual(redact.redact_data(dirty), {"a": [1, CLEAN, {"b": None}], "c": (CLEAN,), "keep": 2.5})
        self.assertIn(STORE_VALUE, dirty["a"][1])                        # the caller's value is not changed

    def test_an_unreadable_store_is_said_once_without_any_value(self) -> None:
        with mock.patch.object(agent_env, "ENV_FILE", self.root):           # a folder: reading it fails
            with self.assertLogs("services.cowork_agent.storm.redact", logging.WARNING) as logs:
                self.assertEqual(redact.redact(AUTH_VALUE), R)              # the rest still works
                redact.redact(AUTH_VALUE)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn(AUTH_VALUE, logs.output[0])


class StoredRecordTests(SecretsCase, unittest.IsolatedAsyncioTestCase):
    """Each kind of record storm writes, through the code that writes it."""

    async def asyncSetUp(self) -> None:
        self.store = await Store.open(self.root / "xo.db")
        self.heard: list = []
        self.store.on_commit(self.heard.extend)
        await self.write(lambda w: w.execute(
            "INSERT INTO sessions (id, seq, role, preset, agent, spec, status, created_at, updated_at) "
            "VALUES ('cse_1', 0, 'thread', 'thread', 'any', '{}', 'cold', ?, ?)", (w.now, w.now)))
        self.chat = await self.write(rows.workspace_chat)

    async def asyncTearDown(self) -> None:
        await self.store.close()

    async def write(self, fn):
        return await self.store.write(lambda tx: fn(Writes(tx, now_iso())))

    async def one(self, sql: str, params: tuple = ()):
        return await self.store.read(lambda conn: conn.execute(sql, params).fetchone())

    async def test_events_are_stored_and_heard_redacted(self) -> None:
        await self.write(lambda w: w.append("step.text", {"text": leaky(), "nested": [{"x": leaky()}]},
                                            session_id="cse_1"))
        (data,) = await self.one("SELECT data FROM events WHERE type = 'step.text'")
        self.assertEqual(json.loads(data), {"text": CLEAN, "nested": [{"x": CLEAN}]})
        self.assertEqual(self.heard[-1].data, {"text": CLEAN, "nested": [{"x": CLEAN}]})

    async def test_notices_and_errors(self) -> None:
        ids, ts = Ids("cse_1"), now_iso()
        await self.write(lambda w: records.step(w, ids, c.Notice(session="cse_1", ts=ts, seq=1, code="unknown_wire",
                                                                 message=leaky()), 1))
        await self.write(lambda w: records.step(w, ids, c.Error(session="cse_1", ts=ts, seq=2, code="protocol",
                                                                message=leaky()), 1))
        await self.write(lambda w: records.set_status(w, ids, "cold", 1, {"reason": "start_failed",
                                                                          "error": leaky()}))
        found = await self.store.read(lambda conn: conn.execute(
            "SELECT type, coalesce(json_extract(data, '$.message'), json_extract(data, '$.error')) FROM events "
            "WHERE type IN ('step.notice', 'step.error', 'session') ORDER BY seq").fetchall())
        self.assertEqual(found, [("step.notice", CLEAN), ("step.error", CLEAN), ("session", CLEAN)])

    async def test_messages_of_chats_and_threads(self) -> None:
        stored = await self.write(lambda w: rows.add_message(w, chat_id=self.chat["id"], author_kind="owner",
                                                             text=leaky(), extra={"quote": leaky()}))
        threaded = await self.write(lambda w: rows.add_message(w, chat_id=self.chat["id"], thread_ts="1.0",
                                                               author_kind="agent", text=leaky()))
        self.assertEqual((stored["text"], stored["extra"]), (CLEAN, {"quote": CLEAN}))   # what an envelope quotes
        for kept in (stored, threaded):
            text, extra = await self.one("SELECT text, extra FROM messages WHERE id = ?", (kept["id"],))
            self.assertEqual(text, CLEAN)
            self.assertNotIn(STORE_VALUE, extra)
        (event,) = await self.one("SELECT data FROM events WHERE type = 'message' ORDER BY seq LIMIT 1")
        self.assertEqual(json.loads(event)["text"], CLEAN)

    async def test_hop_notes(self) -> None:
        hop = await self.write(lambda w: rows.add_hop(w, chat_id=self.chat["id"], thread_ts=None, kind="brief",
                                                      from_role="project-coordinator", from_session="cse_1",
                                                      to_role="thread", to_session=None, note=leaky()))
        self.assertEqual(await self.one("SELECT note FROM hops WHERE id = ?", (hop["id"],)), (CLEAN,))

    async def test_thread_titles_and_status(self) -> None:
        await self.write(lambda w: rows.add_thread(w, chat_id=self.chat["id"], ts="1.0", title=leaky(),
                                                   session_id="cse_1", cwd="/w"))
        await self.write(lambda w: rows.update_thread(w, self.chat["id"], "1.0", status_md=leaky()))
        self.assertEqual(await self.one("SELECT title, status_md FROM threads"), (CLEAN, CLEAN))
        (event,) = await self.one("SELECT data FROM events WHERE type = 'thread' ORDER BY seq DESC LIMIT 1")
        self.assertEqual(json.loads(event)["status_md"], CLEAN)

    async def test_review_items_and_their_annotations(self) -> None:
        queue = ReviewQueue()
        item = await self.write(lambda w: queue.open_item(w, ItemSpec(
            kind="approval", title=leaky(), why=leaky(), origin="agent", session_id="cse_1",
            chat_id=self.chat["id"], detail={"command": leaky(), "paths": [leaky()]})))
        await self.write(lambda w: owner._annotate(w, item, by="coordinator", session="cse_1", note=leaky()))
        title, why, detail, annotations = await self.one(
            "SELECT title, why, detail, annotations FROM review_items WHERE id = ?", (item,))
        self.assertEqual((title, why, json.loads(detail)), (CLEAN, CLEAN, {"command": CLEAN, "paths": [CLEAN]}))
        self.assertEqual(json.loads(annotations)[0]["note"], CLEAN)
        (event,) = await self.one("SELECT data FROM events WHERE type = 'review.opened'")
        self.assertEqual(json.loads(event)["title"], CLEAN)

    async def test_run_titles_and_summaries(self) -> None:
        ids = Ids("cse_1")
        info = c.RunInfo(run_id="task_1", parent_run_id=None, spawn_call_id=None, kind="subagent",
                         agent_type=None, title=leaky(), background=True, depth=1)
        await self.write(lambda w: run_records.started(w, ids, info, turn_id=None, epoch=0, native_id="n1"))
        # The secret sits across the stored summary's limit: redacted first, then cut.
        summary = "x" * (run_records.SUMMARY_MAX - 10) + STORE_VALUE
        await self.write(lambda w: run_records.finished(w, ids, c.RunFinished(
            session="cse_1", ts=now_iso(), seq=3, run="task_1", status="completed", summary=summary),
            epoch=0, progress=None))
        title, stored = await self.one("SELECT title, summary FROM runs")
        self.assertEqual(title, CLEAN)
        self.assertEqual(stored, "x" * (run_records.SUMMARY_MAX - 10) + R)
        self.assertNotIn(STORE_VALUE[:6], stored.replace("x", ""))


class LogTests(SecretsCase):
    def setUp(self) -> None:
        super().setUp()
        redact.redact_logs(redact.STORM_LOGGERS)

    def test_storm_loggers_and_their_children_are_redacted(self) -> None:
        for name in ("services.cowork_agent.storm", "services.cowork_agent.storm.engine.engine",
                     "routers.cowork_agent.storm.chats"):
            with self.subTest(logger=name), self.assertLogs(name, logging.INFO) as logs:
                logging.getLogger(name).info("failed with %s", leaky())
            self.assertEqual(logs.records[0].getMessage(), f"failed with {CLEAN}")

    def test_an_exception_s_text_is_redacted(self) -> None:
        logger = logging.getLogger("services.cowork_agent.storm.probe")
        with self.assertLogs(logger, logging.ERROR) as logs:
            try:
                raise RuntimeError(leaky())
            except RuntimeError:
                logger.exception("it broke")
        self.assertIn(CLEAN, logs.output[0])
        self.assertNotIn(STORE_VALUE, logs.output[0])

    def test_a_runtime_package_named_by_the_engine_is_redacted_and_other_loggers_are_not(self) -> None:
        redact.redact_logs(["tests.made_up_runtime"])
        with self.assertLogs("tests.made_up_runtime.session", logging.INFO) as logs:
            logging.getLogger("tests.made_up_runtime.session").info(leaky())
        self.assertEqual(logs.records[0].getMessage(), CLEAN)
        with self.assertLogs("tests.made_up_runtimes", logging.INFO) as logs:     # a sibling, not a child
            logging.getLogger("tests.made_up_runtimes").info(leaky())
        self.assertEqual(logs.records[0].getMessage(), leaky())


class RawTurnLogTests(SecretsCase, unittest.IsolatedAsyncioTestCase):
    """The per-turn files on their own: which turn a line goes to, bounds,
    modes, failures."""

    def files(self) -> dict[str, list[str]]:
        folder = turn_logs_dir()
        if not folder.exists():
            return {}
        return {p.stem: p.read_bytes().decode("utf-8", "surrogateescape").splitlines()
                for p in sorted(folder.glob("*.jsonl"))}

    async def test_lines_go_to_their_turn_redacted(self) -> None:
        raw = RawTurnLog("cse_1")
        raw.line(b'{"start": 1}')                            # before the first turn: held for it
        raw.begin("turn_a")
        raw.line(json.dumps({"text": leaky()}).encode())
        raw.line(b'{"after": "a"}')                         # between turns: still turn a's
        raw.hold()                                           # an input on its way to the idle CLI
        raw.line(b'{"answer": "b"}')
        raw.begin("turn_b")
        raw.line(b"not json \xff")                           # kept as it is
        raw.hold()
        raw.line(b'{"last": "words"}')                      # the process ends before another turn
        await raw.close()
        files = self.files()
        self.assertEqual(files["turn_a"], ['{"start": 1}', json.dumps({"text": CLEAN}), '{"after": "a"}'])
        self.assertEqual(files["turn_b"][0], '{"answer": "b"}')
        self.assertEqual((turn_logs_dir() / "turn_b.jsonl").read_bytes().splitlines()[1:],
                         [b"not json \xff", b'{"last": "words"}'])
        folder = turn_logs_dir()
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for path in folder.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    async def test_a_turn_file_is_appended_never_rewritten(self) -> None:
        for n in (1, 2):
            raw = RawTurnLog(f"cse_{n}")
            raw.begin("turn_same")
            raw.line(f'{{"process": {n}}}'.encode())
            await raw.close()
        self.assertEqual(self.files()["turn_same"], ['{"process": 1}', '{"process": 2}'])

    async def test_a_process_that_ran_no_turn_writes_nothing(self) -> None:
        raw = RawTurnLog("cse_1")
        raw.line(b'{"init": true}')
        await raw.close()
        self.assertEqual(self.files(), {})

    async def test_memory_is_bounded_and_drops_are_counted(self) -> None:
        raw = RawTurnLog("cse_1")
        with mock.patch.object(raw_turns, "QUEUE_BYTES", 100), mock.patch.object(raw_turns, "HELD_BYTES", 50):
            for _ in range(10):
                raw.line(b"h" * 20)                     # held: two fit, then they are dropped
            raw.begin("turn_a")
            for _ in range(10):
                raw.line(b"q" * 30)                     # queued with nothing written yet
            with self.assertLogs("services.cowork_agent.storm.engine.raw_turns", logging.WARNING) as logs:
                await raw.close()
        lines = self.files()["turn_a"]
        self.assertLess(len(lines), 12)
        self.assertEqual(raw.dropped, 20 - len(lines))
        self.assertIn(f"{raw.dropped} raw CLI line(s)", logs.output[0])

    async def test_a_turn_id_that_cannot_name_a_file_is_not_written(self) -> None:
        raw = RawTurnLog("cse_1")
        raw.begin("../escape")
        raw.line(b"x")
        with self.assertLogs("services.cowork_agent.storm.engine.raw_turns", logging.WARNING):
            await raw.close()
        self.assertFalse(any(self.root.rglob("escape*")))

    async def test_a_failed_write_is_logged_once_and_raises_nothing(self) -> None:
        turn_logs_dir().parent.mkdir(parents=True)
        turn_logs_dir().write_text("a file where the folder goes")      # every write fails
        raw = RawTurnLog("cse_1")
        with self.assertLogs("services.cowork_agent.storm.engine.raw_turns", logging.WARNING) as logs:
            raw.begin("turn_a")
            raw.line(b"one")
            await asyncio.sleep(0.05)
            raw.begin("turn_b")
            raw.line(b"two")
            await raw.close()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("can't be written", logs.output[0])


class EngineTests(unittest.IsolatedAsyncioTestCase):
    """The engine on the fake claude: what a real turn leaves on disk and in
    the store."""

    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.kit.secrets_file.write_text(f"{STORE_KEY}={STORE_VALUE}\n")

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    def started(self, *after: dict) -> list[dict]:
        return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]

    async def test_a_turn_s_raw_output_is_kept_redacted_in_its_own_file(self) -> None:
        kit = self.kit
        said = f"the key is {STORE_VALUE} and {SHAPES[0]}"
        kit.scenario(self.started(*claude.text_turn(SID, said), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1, "hello"))
        finished = await kit.recorder.seen("turn.finished", session_id=sid)
        path = kit.state / "engine" / "turns" / f"{finished.turn_id}.jsonl"
        await kit.until(lambda: path.exists() and '"type": "result"' in path.read_text())
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(lines[0]["type"], "control_response")          # the CLI's start, held for its first turn
        self.assertTrue(any(line.get("subtype") == "init" for line in lines))
        texts = [b["text"] for line in lines if line.get("type") == "assistant"
                 for b in line["message"]["content"]]
        self.assertEqual(texts, [f"the key is {R} and {R}"])
        self.assertNotIn(STORE_VALUE, path.read_text())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        # What the state log keeps of the same turn is redacted too.
        (step,) = kit.recorder.of("step.text", sid)
        self.assertEqual(step.data["text"], f"the key is {R} and {R}")

    async def test_a_raw_log_that_cannot_be_written_never_fails_the_turn(self) -> None:
        kit = self.kit
        (kit.state / "engine").mkdir(parents=True)
        (kit.state / "engine" / "turns").write_text("not a folder")
        kit.scenario(self.started(*claude.text_turn(SID, "hi"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        with self.assertLogs("services.cowork_agent.storm.engine.raw_turns", logging.WARNING) as logs:
            await engine.deliver(sid, message(1, "hello"))
            finished = await kit.recorder.seen("turn.finished", session_id=sid)
            await kit.recorder.seen("session", session_id=sid, status="warm", count=2)
            await engine._live[sid].close("test")
        self.assertEqual(finished.data["status"], "finished")
        self.assertEqual(len(logs.output), 1)

    async def test_the_engine_redacts_its_runtimes_credentials_and_their_logs(self) -> None:
        """The runtime's feature list names its credentials (``auth_env``),
        and its package's loggers are redacted like storm's."""
        kit = self.kit
        kit.scenario([])
        os.environ["ANTHROPIC_AUTH_TOKEN"] = AUTH_VALUE          # inside the kit's own env, put back after
        engine = await kit.boot()
        seq = await engine.write(lambda w: w.append("step.notice", {"message": f"gateway {AUTH_VALUE}"}))
        self.assertEqual(kit.rows("SELECT json_extract(data, '$.message') FROM events WHERE seq = ?", (seq,)),
                         [(f"gateway {R}",)])
        runtime_logger = type(kit.engine._runtimes("claude_code")).__module__ + ".session"
        with self.assertLogs(runtime_logger, logging.INFO) as logs:
            logging.getLogger(runtime_logger).info("stderr said %s", STORE_VALUE)
        self.assertEqual(logs.records[0].getMessage(), f"stderr said {R}")

    async def test_a_live_session_s_comms_token_is_redacted_until_its_process_is_gone(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "hi"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session(mcp_servers=(c.McpServer(name="xo_comms", transport="http",
                                                         url="http://127.0.0.1:9/mcp", bearer_env=SESSION_TOKEN_ENV),))
        await engine.deliver(sid, message(1, "hello"))
        await kit.recorder.seen("turn.finished", session_id=sid)
        token = kit.runs()[0]["env"][SESSION_TOKEN_ENV]

        async def stored(text: str) -> str:
            seq = await engine.write(lambda w: w.append("step.notice", {"message": text}, session_id=sid))
            return kit.rows("SELECT json_extract(data, '$.message') FROM events WHERE seq = ?", (seq,))[0][0]
        self.assertEqual(await stored(f"Authorization: {token}"), f"Authorization: {R}")
        await engine._live[sid].close("test")
        self.assertEqual(await stored(token), token)          # a dead process's token is no secret of a live one



# ── §0.112: the session a message is sent to gets it as typed ────────────────

TYPED = "ghp_" + "Kt9" * 12          # what the person pastes


class WakeScrubTests(unittest.IsolatedAsyncioTestCase):
    """A wake's original text lives in the queue until the wake is done:
    consumed or dropped, it keeps only its ``kept`` text; handed back after a
    crash, it keeps the original (at least once)."""

    async def asyncSetUp(self) -> None:
        self.kit = Kit()

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    def started(self, *after: dict) -> list[dict]:
        return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]

    def wake(self, n: int) -> Wake:
        return Wake(id=f"msg:cmsg_{n}", text=f"use {TYPED} please", kept=f"use {R} please", priority="now",
                    reason="message", origin="person")

    def stored(self, n: int) -> tuple[str, dict]:
        envelope, meta = self.kit.rows("SELECT envelope, meta FROM wakes WHERE id = ?", (f"msg:cmsg_{n}",))[0]
        return envelope, json.loads(meta)

    async def test_the_cli_gets_the_original_and_the_consumed_wake_keeps_the_redacted_text(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, self.wake(1))
        self.assertEqual(self.stored(1)[0], f"use {TYPED} please")      # queued: the original
        await kit.recorder.seen("turn.finished", session_id=sid)
        (run,) = kit.runs()
        self.assertEqual(run["stdin"][1]["message"]["content"][0]["text"], f"use {TYPED} please")
        envelope, meta = self.stored(1)
        self.assertEqual(envelope, f"use {R} please")
        self.assertNotIn("kept", meta)

    async def test_a_wake_the_runtime_refuses_is_dropped_scrubbed(self) -> None:
        kit = self.kit
        kit.scenario(self.started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        with mock.patch.object(claude_session.ClaudeSession, "send",
                               side_effect=c.RuntimeFailure("unsupported", "no such image type")):
            await engine.deliver(sid, self.wake(2))
            await kit.recorder.seen("session", session_id=sid, reason="input_refused")
        self.assertEqual(self.stored(2)[0], f"use {R} please")

    async def test_a_wake_handed_back_after_a_crash_keeps_the_original(self) -> None:
        kit = self.kit
        kit.scenario(self.started({"exit": 1}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, self.wake(1))
        finished = await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(finished.data["wakes_requeued"], ["msg:cmsg_1"])
        envelope, meta = self.stored(1)
        self.assertIn(f"use {TYPED} please", envelope)                   # after the resume note
        self.assertEqual(meta["kept"], f"use {R} please")

    async def test_a_wake_taken_by_a_turn_that_already_finished_is_consumed_scrubbed(self) -> None:
        kit = self.kit
        kit.scenario([])
        engine = await kit.boot()
        sid = await kit.session()

        def take(w: Writes) -> str:
            w.wake(sid, self.wake(1))
            w.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) "
                      "VALUES ('turn_x', ?, 0, 'person', 'finished', ?)", (sid, w.now))
            return records.take_wakes(w, ["msg:cmsg_1"], "turn_x")[0]
        self.assertEqual(await engine.write(take), "consumed")
        self.assertEqual(self.stored(1)[0], f"use {R} please")

    async def test_a_wake_dropped_at_the_redelivery_bound_is_scrubbed(self) -> None:
        kit = self.kit
        kit.scenario([])
        engine = await kit.boot()
        sid = await kit.session()

        def at_the_bound(w: Writes) -> list[str]:
            w.wake(sid, self.wake(1))
            w.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) "
                      "VALUES ('turn_x', ?, 0, 'person', 'failed', ?)", (sid, w.now))
            w.execute("UPDATE wakes SET state = 'taken', turn_id = 'turn_x', redelivery = ?",
                      (MAX_REDELIVERIES,))
            return hand_back(w.tx, "turn_x", "a note", now=w.now, counts=True)[1]
        self.assertEqual(await engine.write(at_the_bound), ["msg:cmsg_1"])
        self.assertEqual(self.stored(1)[0], f"use {R} please")


class TypedTextTests(FlowTestCase):
    """Through the chat routes: the person's words reach the session they
    sent them to as typed; everything stored, relayed or cited is redacted."""

    def inputs(self) -> list[str]:
        """Every user message each fake process read, one string a process."""
        return ["\n".join(block.get("text", "") for line in run.get("stdin", []) if line.get("type") == "user"
                          for block in line["message"]["content"] if isinstance(block, dict))
                for run in self.kit.fake.invocations() if run.get("stdin")]

    async def test_the_coordinator_and_the_thread_get_it_as_typed_and_a_relay_quotes_it_redacted(self) -> None:
        await self.boot_roles({
            PC: [*start("pc", bind="MID"),
                 claude.mcp_call("start_thread_session", {"message_id": "$MID", "title": "Deploy",
                                                          "instructions": "Deploy it."}),
                 *done("pc"), next_input(), claude.mcp_call("no_reply_needed", {"reason": "in the thread"}),
                 *done("pc", 2)],
            TH: [*start("th"), claude.mcp_call("reply", {"text": "Which key?"}), *done("th"),
                 next_input(), claude.mcp_call("reply", {"text": "Deployed."}), *done("th", 2)]})
        ids = await self.chat_ids()
        first = await self.post(ids["demo"], f"Deploy with {TYPED}")
        self.assertEqual(first["text"], f"Deploy with {R}")
        await self.settle(lambda: ("agent", "Which key?") in self.texts(ids["demo"], self.rows(
            "SELECT ts FROM threads")[0][0] if self.rows("SELECT ts FROM threads") else None))
        ts = self.rows("SELECT ts FROM threads")[0][0]
        second_key = "sk-ant-api03-" + "Zq8" * 10
        await self.post(ids["demo"], f"Use {second_key}", thread_ts=ts)
        await self.settle(lambda: ("agent", "Deployed.") in self.texts(ids["demo"], ts))
        await self.settle(lambda: self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'msg:%' AND state = "
                                            "'consumed'")[0][0] == 2)
        coordinator, thread = self.inputs()
        self.assertIn(f"Deploy with {TYPED}", coordinator)               # the session it was sent to
        self.assertNotIn(TYPED, thread)                                  # the brief cites the stored row
        self.assertIn(f"Deploy with {R}", thread)
        self.assertIn(f"Use {second_key}", thread)                      # a message sent to the thread itself
        for (envelope,) in self.rows("SELECT envelope FROM wakes"):
            self.assertNotIn(TYPED, envelope)
            self.assertNotIn(second_key, envelope)
        for (text,) in self.rows("SELECT text FROM messages"):
            self.assertNotIn(TYPED, text)

    async def test_a_decision_model_reads_it_redacted_and_the_coordinator_it_hands_over_to_as_typed(self) -> None:
        await self.boot_roles({PC: [*start("pc"), claude.mcp_call("post_message", {"text": "Hi."}), *done("pc")]})
        await self.http.post("/api/chat/router/models", json={"provider": "jev", "api_key": "k"})
        await self.http.put("/api/chat/router", json={"mode": "decision", "decision_model": "jev"})
        ids = await self.chat_ids()
        asked: list[str] = []

        async def ask(provider, content, questions, *, model, timeout=dm.TIMEOUT_S):
            asked.append(json.dumps(content))
            return dm.Result("jev", {router.INTENT_ID: dm.Answer(router.INTENT_ID, "choice", chosen="chat",
                                                                 probability=0.9, probabilities={"chat": 0.9})})
        with mock.patch.object(dm, "ask", ask):
            await self.post(ids["demo"], f"hello {TYPED}")
            await self.settle(lambda: ("agent", "Hi.") in self.texts(ids["demo"]))
        self.assertTrue(asked)
        self.assertNotIn(TYPED, "".join(asked))
        self.assertIn(R, "".join(asked))
        self.assertIn(f"hello {TYPED}", self.inputs()[0])


# ── review round: answers, relay notes, wakes without kept, review options ─

ANSWER_KEY = "sk-ant-api03-" + "Pw4" * 10      # what the person pastes into an answer
NOTE_KEY = "ghp_" + "Nq2" * 12                   # what a coordinator copies into a brief


def consume(kit: Kit, session_id: str, wake_ids: list[str]):
    """Consume wakes as a turn that already finished does (``take_wakes``)."""
    def take(w: Writes) -> str:
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        w.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) "
                  "VALUES (?, ?, 0, 'wake', 'finished', ?)", (turn_id, session_id, w.now))
        return records.take_wakes(w, wake_ids, turn_id)[0]
    return kit.engine.write(take)


class ConversationScrubTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()                       # no process starts: wakes stay queued until a test consumes them

    def wakes_of(self, session_id: str, like: str) -> list[tuple[str, str, str]]:
        return self.rows("SELECT id, state, envelope FROM wakes WHERE session_id = ? AND id LIKE ?", (session_id, like))

    async def test_a_brief_s_note_reaches_the_thread_redacted(self) -> None:
        chat = await self.project()
        pc = await self.coordinator(chat)
        root = await self.owner_message(chat, "Fix the login page")
        started = await self.call(await self.token(pc), "start_thread_session", message_id=root["id"],
                                  title="Fix login", instructions=f"Fix it; the CI token is {NOTE_KEY}")
        [(wake_id, _state, envelope)] = self.rows("SELECT id, state, envelope FROM wakes WHERE session_id = ?",
                                                  (started["session_id"],))
        self.assertNotIn(NOTE_KEY, envelope)
        self.assertIn(f"the CI token is {R}", envelope)

    async def test_a_consumed_wake_without_kept_is_redacted(self) -> None:
        chat = await self.project()
        pc = await self.coordinator(chat)
        await self.kit.engine.deliver(pc, Wake(id="system:raw", text=f"note {NOTE_KEY}", origin="system"))
        await consume(self.kit, pc, ["system:raw"])
        self.assertEqual(self.wakes_of(pc, "system:raw")[0][1:], ("consumed", f"note {R}"))

    async def test_a_removed_project_drops_its_queued_wakes_scrubbed(self) -> None:
        chat = await self.project("foo")
        pc = await self.coordinator(chat)
        await self.http.post(f"/api/chat/chats/{chat['id']}/messages",
                             json={"text": f"deploy with {ANSWER_KEY}", "client_id": uuid.uuid4().hex})
        [(wake_id, state, envelope)] = self.wakes_of(pc, "msg:%")
        self.assertEqual(state, "queued")
        self.assertIn(ANSWER_KEY, envelope)
        await project_management.remove_project("foo", "foo")
        self.assertEqual(self.wakes_of(pc, "msg:%")[0][1], "dropped")
        self.assertNotIn(ANSWER_KEY, self.wakes_of(pc, "msg:%")[0][2])
        retired = [e for e in self.kit.recorder.of("session", pc) if e.data.get("reason") == "retired"]
        self.assertEqual(retired[-1].data["wakes_dropped"], [wake_id])


if __name__ == "__main__":
    unittest.main()
