"""agy usage: its own per-call usage records (fresh input, cache reads and
writes, output), each call on its own step's day, failed calls counted as
unreported (never as zero), and sub-agents' calls under their root conversation."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.antigravity import tokens as agy_tokens
from services.cowork_agent.adapters.antigravity import transcript as agy_transcript
from services.cowork_agent.adapters import usage_common as _uc
from services.cowork_agent.adapters.antigravity import usage as agy_usage

USER_INPUT, PLANNER_RESPONSE = agy_tokens.USER_INPUT, agy_tokens.PLANNER_RESPONSE
MODEL = "Gemini 3.5 Flash (Low)"


# ── A minimal protobuf writer for agy's store rows ────────────────────────────

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def pb(*fields: tuple[int, object]) -> bytes:
    """Fields as (number, int | bytes | str): ints as varints, the rest length-delimited."""
    out = bytearray()
    for number, value in fields:
        if isinstance(value, int):
            out += _varint(number << 3) + _varint(value)
        else:
            raw = value.encode() if isinstance(value, str) else value
            out += _varint(number << 3 | 2) + _varint(len(raw)) + raw
    return bytes(out)


def at(stamp: str) -> float:
    return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()


def step_row(idx: int, kind: int, when: str) -> tuple:
    seconds = at(when)
    return idx, kind, pb((1, pb((1, int(seconds)), (2, 250_000_000))))


def call_row(idx: int, *, fresh=None, output=None, cache_write=None, cache_read=None, reasoning=None,
             message_id=None, steps=(), model=MODEL) -> tuple:
    """A gen_metadata row; a call with no counts is one that failed before the provider answered."""
    usage = [(n, v) for n, v in ((2, fresh), (3, output), (4, cache_write), (5, cache_read), (9, reasoning))
             if v is not None]
    if message_id:
        usage.append((7, message_id))
    fields: list = [(1, pb((4, pb(*usage)), (21, model)))]
    if steps:
        fields.append((2, b"".join(_varint(s) for s in steps)))
    if not usage:
        fields.append((5, pb((1, "quota exceeded"))))
    return idx, pb(*fields)


class _Nov27(datetime):
    """``datetime`` whose today is 2026-11-27 (noon UTC)."""

    @classmethod
    def now(cls, tz=None):
        moment = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)


class AgyStore:
    """A throwaway agy home: conversation stores, transcripts and the summaries index."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.conversations = root / "conversations"
        self.conversations.mkdir()
        self.summaries = root / "conversation_summaries.db"
        con = sqlite3.connect(self.summaries)
        con.execute("CREATE TABLE conversation_summaries (conversation_id text, title text, "
                    "last_modified_time datetime, workspace_uris text, nesting_depth integer, "
                    "parent_conversation_id text)")
        con.commit()
        con.close()

    def transcript(self, conversation_id: str) -> Path:
        return self.root / "brain" / f"{conversation_id}.jsonl"

    def add(self, conversation_id: str, *, steps: list, calls: list, changed: str, folder="/work/app",
            parent: str | None = None, transcript: list | None = None) -> None:
        con = sqlite3.connect(self.conversations / f"{conversation_id}.db")
        con.execute("CREATE TABLE steps (idx integer, step_type integer, metadata blob)")
        con.execute("CREATE TABLE gen_metadata (idx integer, data blob)")
        con.executemany("INSERT INTO steps VALUES (?, ?, ?)", steps)
        con.executemany("INSERT INTO gen_metadata VALUES (?, ?)", calls)
        con.commit()
        con.close()
        con = sqlite3.connect(self.summaries)
        con.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?)",
                    (conversation_id, "secret title words", changed, json.dumps([f"file://{folder}"]),
                     1 if parent else 0, parent))
        con.commit()
        con.close()
        if transcript is not None:
            path = self.transcript(conversation_id)
            path.parent.mkdir(exist_ok=True)
            path.write_text("".join(json.dumps(line) + "\n" for line in transcript))

    def patch(self, case: unittest.TestCase, *more: tuple) -> None:
        targets = [
            (agy_usage, "CONVERSATIONS_DIR", self.conversations),
            (agy_usage, "transcript_path", self.transcript),
            (agy_tokens, "conversation_db", lambda cid: self.conversations / f"{cid}.db"),
            (agy_transcript, "_SUMMARIES_DB", self.summaries),
            (agy_usage, "_parents_kept", ((), {})),
            *more,
        ]
        for module, name, value in targets:
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            case.addCleanup(patcher.stop)
        agy_usage._kept.clear()
        agy_tokens._kept.clear()
        case.addCleanup(agy_usage._kept.clear)
        case.addCleanup(agy_tokens._kept.clear)


def build_fixture(store: AgyStore) -> None:
    """c1: a prompt on 11-20 and one on 11-21, a call after each, then a failed call;
    c2: a sub-agent c1 ran on 11-21, with one call."""
    store.add("c1", changed="2026-11-21 09:05:00+00:00", steps=[
        step_row(0, USER_INPUT, "2026-11-20T10:00:00"),
        step_row(1, PLANNER_RESPONSE, "2026-11-20T10:00:05"),
        step_row(2, USER_INPUT, "2026-11-21T09:00:00"),
        step_row(3, PLANNER_RESPONSE, "2026-11-21T09:00:03"),
    ], calls=[
        call_row(0, fresh=100, output=20, reasoning=5, cache_write=7, cache_read=1000, message_id="m-a", steps=(1,)),
        call_row(1, fresh=50, output=10, cache_read=2000, message_id="m-b", steps=(3, 4)),
        call_row(2),
    ], transcript=[
        {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-20T10:00:00Z", "content": "secret prompt"},
        {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-11-20T10:00:05Z"},
    ])
    store.add("c2", parent="c1", changed="2026-11-21 09:00:20+00:00", steps=[
        step_row(0, PLANNER_RESPONSE, "2026-11-21T09:00:10"),
    ], calls=[call_row(0, fresh=30, output=3, message_id="m-c", steps=(0,))])


class AgyUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = AgyStore(Path(tmp.name))
        build_fixture(self.store)
        self.store.patch(self)

    def test_each_call_is_read_with_its_own_step_time_and_a_failed_call_is_unreported(self) -> None:
        conversation = agy_tokens.read_conversation(self.store.conversations / "c1.db")
        first, second, failed = conversation.calls
        self.assertEqual((first.fresh_input, first.output, first.cache_write, first.cache_read, first.reasoning),
                         (100, 20, 7, 1000, 5))
        self.assertEqual((first.step, second.step), (1, 3))
        self.assertAlmostEqual(first.at, at("2026-11-20T10:00:05") + 0.25)
        self.assertAlmostEqual(second.at, at("2026-11-21T09:00:03") + 0.25)
        self.assertFalse(failed.reported)
        self.assertEqual(failed.at, second.at)     # no step of its own: dated by the call before it
        self.assertEqual(len(conversation.user_times), 2)

    def test_parse_file_gives_an_entry_per_prompt_and_per_reported_call(self) -> None:
        meta, entries = agy_usage.parse_file(str(self.store.conversations / "c1.db"))
        self.assertEqual([e["role"] for e in entries], ["user", "assistant", "user", "assistant"])
        reply = entries[1]
        self.assertEqual(reply["usage"]["input"], 100)
        self.assertEqual(reply["usage"]["totalTokens"], 100 + 20 + 7 + 1000)
        self.assertEqual(reply["usage"]["cost"], {})        # unknown, not free
        self.assertEqual((reply["provider"], reply["messageId"], reply["durationMs"]), ("google", "m-a", 5000))
        self.assertEqual(meta["unreportedCalls"], 1)
        self.assertEqual(meta["sessionId"], "c1")

    def test_a_sub_agent_counts_under_its_root_conversation(self) -> None:
        meta, _entries = agy_usage.parse_file(str(self.store.conversations / "c2.db"))
        self.assertEqual(meta["sessionId"], "c1")
        self.assertEqual(agy_usage.list_sessions()["count"], 1)

    def test_sync_days_follow_each_calls_own_day(self) -> None:
        payload = agy_usage.sync_payload(since_date=None)
        day20, day21 = payload["2026-11-20"], payload["2026-11-21"]
        self.assertEqual((day20["total_input_tokens"], day20["total_output_tokens"],
                          day20["total_cache_read_tokens"], day20["total_cache_write_tokens"]), (100, 20, 1000, 7))
        self.assertEqual((day21["total_input_tokens"], day21["total_output_tokens"],
                          day21["total_cache_read_tokens"], day21["total_cache_write_tokens"]), (80, 13, 2000, 0))
        self.assertEqual(day21["total_messages"], 3)        # one prompt, c1's call, the sub-agent's call
        self.assertEqual(payload["__session_dates__"], {"2026-11-20": 1, "2026-11-21": 1})
        self.assertEqual(day20["total_cost"], 0.0)

    def test_dashboard_rows_have_the_shared_shape_and_count_the_unreported_call(self) -> None:
        with mock.patch.object(agy_usage, "datetime", _Nov27), mock.patch.object(_uc, "datetime", _Nov27):
            stats = agy_usage.dashboard(window={"days": 30, "tz": "utc"})
            week = agy_usage.dashboard(window={"days": 7, "tz": "utc"})
        tokens = stats["total_tokens"]
        self.assertEqual((tokens["input"], tokens["output"], tokens["cache_read"], tokens["cache_write"]),
                         (180, 33, 3000, 7))
        daily = {row["date"]: row["tokens"] for row in stats["daily"] if row["messages"]}
        self.assertEqual(daily, {"2026-11-20": 120, "2026-11-21": 93})
        self.assertEqual({row["date"] for row in week["daily"] if row["messages"]}, {"2026-11-21"})
        self.assertEqual(week["unreported_calls"], 1)
        self.assertEqual(stats["unreported_calls"], 1)
        self.assertIs(stats["estimated_tokens"], False)
        [model] = stats["by_model"]
        self.assertEqual((model["model_id"], model["provider_id"]), (MODEL, "google"))

    def test_the_figures_are_read_again_once_agy_writes_the_store(self) -> None:
        agy_usage.parse_file(str(self.store.conversations / "c1.db"))
        con = sqlite3.connect(self.store.conversations / "c1.db")
        con.execute("INSERT INTO gen_metadata VALUES (?, ?)",
                    call_row(3, fresh=1, output=1, message_id="m-d", steps=(3,)))
        con.commit()
        con.close()
        _meta, entries = agy_usage.parse_file(str(self.store.conversations / "c1.db"))
        self.assertEqual(sum(1 for e in entries if e["role"] == "assistant"), 3)

    def test_a_store_it_cant_read_is_never_kept(self) -> None:
        db = self.store.conversations / "c1.db"
        con = sqlite3.connect(db)
        con.execute("ALTER TABLE steps RENAME TO steps_v2")        # an agy update changed its tables
        con.commit()
        con.close()
        self.assertIs(agy_tokens.read_conversation(db), agy_tokens.UNREAD)
        _meta, entries = agy_usage.parse_file(str(db))
        self.assertEqual([e for e in entries if e["role"] == "assistant"], [])
        con = sqlite3.connect(db)
        con.execute("ALTER TABLE steps_v2 RENAME TO steps")
        con.commit()
        con.close()
        _meta, entries = agy_usage.parse_file(str(db))             # nothing was kept: read as it is now
        self.assertEqual(sum(1 for e in entries if e["role"] == "assistant"), 2)


if __name__ == "__main__":
    unittest.main()
