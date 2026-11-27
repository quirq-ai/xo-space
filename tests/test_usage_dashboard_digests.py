"""Usage for claude_code and codex: the /api/usage dashboard from each
transcript's kept digest, and the shared entry views, against transcripts
shaped as the CLIs wrote them on 11-27 (sub-agents, copied records, codex's
per-response records and repeated token counts, text the CLI adds itself)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import usage_common as uc
from services.cowork_agent.adapters.claude_code import usage as claude
from services.cowork_agent.adapters.codex import usage as codex
from services.cowork_agent.engine.usage_loader import AllRuntimesUsage


def _at(days_ago: int, hour: int, minute: int = 0, second: int = 0) -> str:
    """A UTC stamp ``days_ago`` days before today (UTC), as the CLIs write it."""
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return (day + timedelta(hours=hour, minutes=minute, seconds=second)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write(path: Path, records: list[dict]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(path)


def _days_total(dashboard: dict) -> dict:
    """The day rows' token split added up, to set against the window's ``total_tokens``."""
    total = uc.empty_tokens()
    for day in dashboard["daily"]:
        for key, value in day["total_tokens"].items():
            total[key] += value
    return total


def _user(ts: str, content) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": content}}


def _reply(ts: str, msg_id: str, inp: int, out: int, *, model: str = "claude-opus-5-5", cache_r: int = 0) -> dict:
    return {"type": "assistant", "timestamp": ts, "message": {
        "id": msg_id, "model": model, "role": "assistant",
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_r,
                  "cache_creation_input_tokens": 0}}}


class ClaudeDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for patch in (mock.patch.dict(os.environ, {"CLAUDE_PROJECTS_DIR": str(self.root)}),
                      mock.patch.object(claude, "_DIGESTS", uc.Digests(claude._digest))):
            patch.start()
            self.addCleanup(patch.stop)

    def dashboard(self, days: int) -> dict:
        return claude.aggregate_for_dashboard(days=days, tz="utc")

    def test_a_message_streamed_in_parts_counts_once_and_a_tool_result_times_the_reply(self) -> None:
        _write(self.root / "p" / "s1.jsonl", [
            _user(_at(0, 9), "hello"),
            _reply(_at(0, 9, 0, 5), "m1", 100, 10, cache_r=50),
            _reply(_at(0, 9, 0, 6), "m1", 100, 10, cache_r=50),        # the same message, another part
            _user(_at(0, 9, 1), [{"type": "tool_result", "content": "ok"}]),
            _reply(_at(0, 9, 1, 3), "m2", 200, 20),
        ])
        d = self.dashboard(1)
        self.assertEqual(d["total_tokens"], {"input": 300, "output": 30, "reasoning": 0, "cache_read": 50,
                                             "cache_write": 0})
        self.assertEqual(d["total_messages"], 3)           # two replies, one message a person wrote
        self.assertEqual(d["total_sessions"], 1)
        self.assertEqual(d["response_time"]["count"], 2)
        self.assertEqual((d["response_time"]["min"], d["response_time"]["max"]), (3.0, 5.0))
        self.assertEqual(d["daily"][-1]["tokens"], 330)
        self.assertEqual(d["daily"][-1]["total_tokens"], d["total_tokens"])     # the day's split
        self.assertEqual(d["by_session"][0]["time_created"], _at(0, 9))

    def test_the_window_starts_at_midnight_and_a_message_first_seen_before_it_is_counted_inside_it(self) -> None:
        _write(self.root / "p" / "s1.jsonl", [
            _reply(_at(1, 23, 59, 59), "m1", 7, 1),                 # yesterday: outside a one-day window
            _reply(_at(0, 0, 0, 0), "m1", 7, 1),                    # its next part, at midnight: inside
            _reply(_at(0, 0, 0, 1), "m1", 7, 1),                    # and again: already counted
        ])
        self.assertEqual(self.dashboard(1)["total_tokens"]["input"], 7)
        self.assertEqual(self.dashboard(2)["total_tokens"]["input"], 7)
        self.assertEqual([row["tokens"] for row in self.dashboard(2)["daily"]], [8, 0])
        two_days = self.dashboard(2)
        self.assertEqual([row["total_tokens"]["input"] for row in two_days["daily"]], [7, 0])
        self.assertEqual(_days_total(two_days), two_days["total_tokens"])
        before, _yesterday, today = self.dashboard(3)["daily"]
        self.assertEqual(today["total_tokens"], uc.empty_tokens())
        self.assertIsNot(before["total_tokens"], today["total_tokens"])   # each empty day has a split of its own

    def test_a_reply_more_than_ten_minutes_after_the_prompt_is_not_timed(self) -> None:
        _write(self.root / "p" / "s1.jsonl", [_user(_at(0, 9), "go"), _reply(_at(0, 9, 10, 1), "m1", 1, 1)])
        self.assertEqual(self.dashboard(1)["response_time"]["count"], 0)

    def test_each_file_is_read_once_until_it_changes_and_let_go_once_it_is_gone(self) -> None:
        path = _write(self.root / "p" / "s1.jsonl", [_user(_at(0, 9), "go"), _reply(_at(0, 9, 0, 1), "m1", 5, 1)])
        reads = []
        digests = uc.Digests(lambda p: reads.append(p) or claude._digest(p))
        with mock.patch.object(claude, "_DIGESTS", digests):
            self.dashboard(1)
            self.dashboard(30)
            self.assertEqual(reads, [path])
            with open(path, "a") as stream:
                stream.write(json.dumps(_reply(_at(0, 9, 0, 9), "m2", 6, 1)) + "\n")
            self.assertEqual(self.dashboard(1)["total_tokens"]["input"], 11)
            self.assertEqual(reads, [path, path])
            os.unlink(path)
            self.assertEqual(self.dashboard(1)["total_messages"], 0)
            self.assertEqual(digests._kept, {})

    def test_a_time_without_a_zone_is_left_out_rather_than_failing_the_dashboard(self) -> None:
        _write(self.root / "p" / "s1.jsonl", [_reply("2026-11-27T09:00:00", "m0", 9, 9),
                                              _reply(_at(0, 9), "m1", 5, 1)])
        self.assertEqual(self.dashboard(1)["total_tokens"]["input"], 5)


class ClaudeAccuracyTests(ClaudeDashboardTests):
    """What counts: every model call once (in any file), sub-agents in their
    session, only messages sent to the session, days in the window's zone."""

    def sub_agent(self, records: list[dict]) -> str:
        for r in records:
            r.update(isSidechain=True, sessionId="s1")
        return _write(self.root / "p" / "s1" / "subagents" / "agent-a1.jsonl", records)

    def test_a_sub_agent_s_calls_count_in_its_session_and_a_copied_call_counts_once(self) -> None:
        _write(self.root / "p" / "s1.jsonl", [_user(_at(0, 9), "go"), _reply(_at(0, 9, 0, 2), "m1", 100, 10)])
        self.sub_agent([_user(_at(0, 9, 1), "look into it"),
                        _reply(_at(0, 9, 0, 2), "m1", 100, 10),              # the parent's call, copied
                        _reply(_at(0, 9, 1, 5), "m2", 40, 4)])
        d = self.dashboard(1)
        self.assertEqual((d["total_tokens"]["input"], d["total_tokens"]["output"]), (140, 14))
        self.assertEqual(d["total_messages"], 3)          # two calls and the person's message, not the sub-agent's prompt
        self.assertEqual(d["total_sessions"], 1)
        self.assertEqual([(s["session_id"], s["title"], s["total_tokens"], s["message_count"]) for s in d["by_session"]],
                         [("s1", "go", 154, 2)])
        self.assertEqual(claude.summary(window={"days": 1, "tz": "utc"})["sessionCount"], 1)
        self.assertEqual(claude.summary(window={"days": 1, "tz": "utc"})["totalTokens"], 154)
        self.assertEqual(claude.summary_card(window={"days": 1, "tz": "utc"})["totalTokens"], 154)
        self.assertEqual([(r["sessionId"], r["messageCount"]) for r in claude.list_sessions()["sessions"]], [("s1", 2)])
        self.assertEqual(claude.get_session("s1")["totalTokens"], 154)
        day = claude.aggregate_for_sync()
        self.assertEqual(day["__session_dates__"], {_at(0, 9)[:10]: 1})
        self.assertEqual(day[_at(0, 9)[:10]]["total_output_tokens"], 14)

    def test_a_message_copied_into_another_file_of_its_session_counts_once(self) -> None:
        sent = {**_user(_at(0, 9), "go"), "uuid": "u1", "sessionId": "s1"}
        call = {**_reply(_at(0, 9, 0, 2), "m1", 100, 10), "sessionId": "s1"}
        _write(self.root / "p" / "s1.jsonl", [sent, call])
        _write(self.root / "q" / "s1.jsonl", [sent, call])              # the same session in a second folder
        self.assertEqual(self.dashboard(1)["total_messages"], 2)
        window = {"days": 1, "tz": "utc"}
        self.assertEqual(claude.summary(window=window)["messageCounts"], {**claude.summary(window=window)["messageCounts"],
                                                                         "user": 1, "assistant": 1, "total": 2})
        self.assertEqual(claude.get_session("s1")["messageCounts"]["user"], 1)

    def test_only_messages_sent_to_the_session_count_and_an_error_record_is_no_call(self) -> None:
        _write(self.root / "p" / "s1.jsonl", [
            _user(_at(0, 9), "<relay from=\"owner\">please</relay>"),
            {**_user(_at(0, 9, 0, 1), "[Image: a.png]"), "isMeta": True},
            _user(_at(0, 9, 0, 2), "[Request interrupted by user]"),
            _user(_at(0, 9, 0, 3), [{"type": "text", "text": "<task-notification>done</task-notification>"}]),
            {**_user(_at(0, 9, 0, 4), "This session is being continued"), "isCompactSummary": True},
            {"type": "assistant", "timestamp": _at(0, 9, 0, 5), "message": {
                "id": "e1", "model": "<synthetic>", "usage": {"input_tokens": 0, "output_tokens": 0}}},
            _reply(_at(0, 9, 0, 6), "m1", 5, 1),
        ])
        d = self.dashboard(1)
        self.assertEqual(d["total_messages"], 2)
        self.assertEqual([m["model_id"] for m in d["by_model"]], ["claude-opus-5-5"])
        self.assertEqual(claude.summary(window={"days": 1, "tz": "utc"})["messageCounts"]["user"], 1)

    def test_days_are_calendar_days_in_the_window_s_zone(self) -> None:
        india = timezone(timedelta(hours=5, minutes=30))
        _write(self.root / "p" / "s1.jsonl", [_reply(_at(0, 0, 0, 0), "m1", 5, 1)])   # 05:30 in India, today
        digests = uc.Digests(claude._digest)
        d = uc.dashboard_from_digests(digests, claude._discover(), days=2, zone=india, provider="anthropic")
        local_day = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                     .astimezone(india).date().isoformat())
        self.assertEqual({r["date"]: r["tokens"] for r in d["daily"] if r["tokens"]}, {local_day: 6})


class CodexDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.rollout = self.root / "rollout-2026-11-27T09-00-00-0199aaaa-bbbb-7ccc-8ddd-eeeeffff0000.jsonl"
        for patch in (mock.patch.object(codex, "_discover", lambda: [str(self.rollout)]),
                      mock.patch.object(codex, "_DIGESTS", uc.Digests(codex._digest))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_turns_are_counted_by_their_last_usage_under_the_model_then_in_use(self) -> None:
        def tokens(ts, inp, cached, out):
            return {"type": "event_msg", "timestamp": ts, "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}}
        _write(self.rollout, [
            {"type": "session_meta", "timestamp": _at(0, 9), "payload": {"session_id": "native-1"}},
            {"type": "turn_context", "timestamp": _at(0, 9), "payload": {"model": "gpt-5.5"}},
            {"type": "event_msg", "timestamp": _at(0, 9), "payload": {"type": "user_message", "message": "go"}},
            tokens(_at(0, 9, 0, 4), 1000, 600, 50),
            {"type": "event_msg", "timestamp": _at(0, 9, 1),
             "payload": {"type": "token_count", "info": None}},           # a limits-only update
            {"type": "turn_context", "timestamp": _at(0, 9, 2), "payload": {"model": "gpt-5.5-mini"}},
            tokens(_at(0, 9, 2, 1), 10, 0, 1),
        ])
        d = codex.aggregate_for_dashboard(days=1, tz="utc")
        self.assertEqual(d["total_tokens"], {"input": 410, "output": 51, "reasoning": 0, "cache_read": 600,
                                             "cache_write": 0})
        self.assertEqual([(m["model_id"], m["message_count"]) for m in d["by_model"]],
                         [("gpt-5.5", 1), ("gpt-5.5-mini", 1)])
        self.assertEqual(d["by_session"][0]["session_id"], "native-1")
        self.assertEqual(d["daily"][-1]["tokens"], 461)         # fresh input + output, as claude's
        self.assertEqual(d["daily"][-1]["total_tokens"], d["total_tokens"])      # cached input in cache_read
        self.assertEqual(d["response_time"]["avg"], 4.0)


class DailySplitTests(unittest.TestCase):
    """Each day's token split adds up to the window's ``total_tokens``, one runtime or two merged."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.rollout = root / "rollout-2026-11-27T09-00-00-0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001.jsonl"
        for patch in (mock.patch.dict(os.environ, {"CLAUDE_PROJECTS_DIR": str(root / "claude")}),
                      mock.patch.object(claude, "_DIGESTS", uc.Digests(claude._digest)),
                      mock.patch.object(codex, "_discover", lambda: [str(self.rollout)]),
                      mock.patch.object(codex, "_DIGESTS", uc.Digests(codex._digest))):
            patch.start()
            self.addCleanup(patch.stop)
        _write(root / "claude" / "p" / "s1.jsonl", [
            _reply(_at(1, 9), "m1", 100, 10, cache_r=40),
            _reply(_at(0, 9), "m2", 200, 20, cache_r=80),
        ])

        def tokens(ts, inp, cached, out):
            return {"type": "event_msg", "timestamp": ts, "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
                                     "cache_write_input_tokens": 3}}}}
        _write(self.rollout, [
            {"type": "turn_context", "timestamp": _at(0, 8), "payload": {"model": "gpt-5.5"}},
            tokens(_at(0, 9, 30), 1000, 600, 50),
        ])

    def test_two_runtimes_merged_keep_each_day_s_split_adding_up_to_the_headline(self) -> None:
        window = {"days": 2, "tz": "utc"}
        alone = codex.dashboard(window=window)
        self.assertEqual(_days_total(alone), alone["total_tokens"])
        merged = AllRuntimesUsage([("claude_code", claude), ("codex", codex)]).dashboard(window=window)
        self.assertEqual([day["total_tokens"] for day in merged["daily"]], [
            {"input": 100, "output": 10, "reasoning": 0, "cache_read": 40, "cache_write": 0},
            {"input": 600, "output": 70, "reasoning": 0, "cache_read": 680, "cache_write": 3},
        ])
        self.assertEqual(_days_total(merged), merged["total_tokens"])
        self.assertEqual([day["tokens"] for day in merged["daily"]], [110, 670])   # unchanged: fresh in + out


class CodexDiscoveryTests(unittest.TestCase):
    def test_usage_reads_archived_threads_and_the_session_list_does_not(self) -> None:
        from services.cowork_agent.adapters.codex import paths
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"CODEX_HOME": home}):
            live = _write(Path(home) / "sessions" / "2026" / "11" / "27" / "rollout-2026-11-27T09-00-00-a.jsonl", [])
            archived = _write(Path(home) / "archived_sessions" / "rollout-2026-11-20T09-00-00-b.jsonl", [])
            self.assertEqual(codex._discover(), sorted([str(live), str(archived)]))
            self.assertEqual([str(p) for p in paths.iter_rollouts()], [str(live)])


class CodexAccuracyTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.files: list[str] = []
        for patch in (mock.patch.object(codex, "_discover", lambda: list(self.files)),
                      mock.patch.object(codex, "_DIGESTS", uc.Digests(codex._digest))):
            patch.start()
            self.addCleanup(patch.stop)

    def rollout(self, uuid: str, session_id: str, records: list[dict]) -> None:
        meta = {"type": "session_meta", "timestamp": _at(0, 9), "payload": {"id": uuid, "session_id": session_id}}
        self.files.append(_write(self.root / f"rollout-2026-11-27T09-00-00-{uuid}.jsonl", [meta, *records]))

    @staticmethod
    def user(ts: str, text: str) -> dict:
        return {"type": "event_msg", "timestamp": ts, "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "id": f"u-{ts}", "content": [{"type": "text", "text": text, "text_elements": []}]}}}

    @staticmethod
    def call(ts: str, response: str, inp: int, cached: int, out: int, turn: str = "t1") -> dict:
        return {"type": "token_usage_record", "timestamp": ts, "payload": {
            "turn_id": turn, "response_id": response, "usage": {
                "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}

    @staticmethod
    def count(ts: str, inp: int, cached: int, out: int, total: int) -> dict:
        usage = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}
        return {"type": "event_msg", "timestamp": ts, "payload": {"type": "token_count", "info": {
            "last_token_usage": usage, "total_token_usage": {**usage, "total_tokens": total}}}}

    def test_each_response_counts_once_compaction_included_and_token_counts_are_not_added_again(self) -> None:
        root = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
        self.rollout(root, root, [
            {"type": "turn_context", "timestamp": _at(0, 9), "payload": {"turn_id": "t1", "model": "gpt-6.2-sol"}},
            self.user(_at(0, 9, 0, 1), "fix the build"),
            self.call(_at(0, 9, 0, 3), "resp_1", 1000, 600, 50),
            self.count(_at(0, 9, 0, 3), 1000, 600, 50, 1050),
            self.count(_at(0, 9, 0, 4), 1000, 600, 50, 1050),                  # repeated with a limits update
            self.call(_at(0, 9, 0, 9), "resp_2", 300, 0, 20),                   # a compaction call
        ])
        d = codex.aggregate_for_dashboard(days=1, tz="utc")
        self.assertEqual((d["total_tokens"]["input"], d["total_tokens"]["cache_read"], d["total_tokens"]["output"]),
                         (700, 600, 70))
        self.assertEqual(d["total_messages"], 3)
        self.assertEqual(d["by_session"][0]["title"], "fix the build")
        self.assertEqual(d["response_time"]["count"], 1)
        self.assertEqual(codex.summary(window={"days": 1, "tz": "utc"})["totalTokens"], 1370)

    def test_without_per_response_records_a_repeated_count_and_a_context_reset_are_skipped(self) -> None:
        root = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0002"
        self.rollout(root, root, [
            self.count(_at(0, 9, 0, 3), 1000, 600, 50, 1050),
            self.count(_at(0, 9, 0, 4), 1000, 600, 50, 1050),
            self.count(_at(0, 9, 0, 5), 0, 0, 0, 1050),                       # the context after compaction
            self.count(_at(0, 9, 0, 9), 200, 100, 5, 1255),
        ])
        d = codex.aggregate_for_dashboard(days=1, tz="utc")
        self.assertEqual((d["total_tokens"]["input"], d["total_tokens"]["output"], d["total_messages"]), (500, 55, 2))

    def test_a_rollout_resumed_on_a_codex_that_writes_records_keeps_its_earlier_counts(self) -> None:
        """Round 6: any record dropped every ``token_count`` call, the ones before records began included."""
        root = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0007"
        self.rollout(root, root, [
            self.count(_at(0, 9, 0, 3), 1000, 600, 50, 1050),                 # an older codex: counts only
            self.call(_at(0, 9, 5, 0), "resp_1", 300, 100, 20),               # resumed on one that writes records
            self.count(_at(0, 9, 5, 0), 300, 100, 20, 1370),                  # repeats the record before it
        ])
        d = codex.aggregate_for_dashboard(days=1, tz="utc")
        self.assertEqual((d["total_tokens"]["input"], d["total_tokens"]["output"], d["total_messages"]), (600, 70, 2))

    def test_a_fork_s_copy_of_older_lines_counts_once(self) -> None:
        """Round 6: an older codex's calls and messages had no key, so a fork's copy of them counted twice."""
        root, fork = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0008", "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0009"
        message = {"type": "event_msg", "timestamp": _at(0, 9), "payload": {"type": "user_message",
                                                                             "message": "plan it"}}
        history = [message, self.count(_at(0, 9, 0, 2), 100, 0, 10, 110)]
        self.rollout(root, root, history)
        self.rollout(fork, root, [*history, self.count(_at(0, 9, 1, 2), 50, 0, 5, 165)])
        d = codex.aggregate_for_dashboard(days=1, tz="utc")
        self.assertEqual((d["total_messages"], d["total_tokens"]["input"], d["total_tokens"]["output"]), (3, 150, 15))
        self.assertEqual(codex.summary(window={"days": 1, "tz": "utc"})["messageCounts"]["user"], 1)

    def test_a_sub_agent_s_rollout_counts_in_its_root_session(self) -> None:
        root, child = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0003", "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0004"
        self.rollout(root, root, [self.user(_at(0, 9), "plan it"), self.call(_at(0, 9, 0, 2), "r1", 100, 0, 10)])
        self.rollout(child, root, [self.user(_at(0, 9, 1), "sub-task"), self.call(_at(0, 9, 1, 2), "r2", 50, 0, 5)])
        d = codex.aggregate_for_dashboard(days=1, tz="utc")
        self.assertEqual(d["total_sessions"], 1)
        self.assertEqual([(s["session_id"], s["title"], s["total_tokens"]) for s in d["by_session"]],
                         [(root, "plan it", 165)])
        self.assertEqual([r["sessionId"] for r in codex.list_sessions()["sessions"]], [root])
        self.assertEqual(codex.get_session(root)["totalTokens"], 165)

    def test_a_fork_s_copy_of_its_parent_s_turns_counts_once(self) -> None:
        root, fork = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0005", "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0006"
        history = [self.user(_at(0, 9), "plan it"), self.call(_at(0, 9, 0, 2), "r1", 100, 0, 10)]
        self.rollout(root, root, history)
        self.rollout(fork, root, [*history, self.call(_at(0, 9, 1, 2), "r2", 50, 0, 5)])
        d = codex.aggregate_for_dashboard(days=1, tz="utc")
        self.assertEqual((d["total_messages"], d["total_tokens"]["input"], d["total_tokens"]["output"]), (3, 150, 15))
        window = {"days": 1, "tz": "utc"}
        self.assertEqual(codex.summary(window=window)["messageCounts"]["user"], 1)
        self.assertEqual(codex.get_session(root)["messageCounts"]["user"], 1)


if __name__ == "__main__":
    unittest.main()
