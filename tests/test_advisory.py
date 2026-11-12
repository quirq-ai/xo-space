"""The space advisory: facts only, each with its source and age, and stale
shown as unknown.

* the usage book's rules: resets rounded to the minute, usage only rising
  within a window, a report with no number keeping the last, an earlier
  window dropped, a reset never assumed, paid facts kept until changed;
* an account named by its agent and account label (None its own account);
* one writer, versioned snapshots, change listeners, ``wait_newer``;
* the engine's calls never raise into it;
* the machine and the sessions, from recorded ``/proc`` and cgroup files;
* its own model-free reads, and their pace (the vendor's words, no threshold).

Recorded payloads: the 11-09/10 jitter (one reset a second apart) and the
99 %-after-100 % flap, as the paid-usage incident found them.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from services.advisory.advisory import READ_CALM_S, READ_NEAR_S, Advisory
from services.advisory.machine import Sample, cgroup_dir, machine, parse_cpu_max, parse_pressure, read_sample
from services.advisory.sessions import Root, measure
from services.cowork_agent.adapters import contract as c

T0 = 1_794_400_000.0                     # 2026-11-11T12:26:40Z
ACCOUNT = ("codex", "acct-4d3aa4c0d340")
WEEK_RESET = "2026-11-16T06:52:23Z"      # the incident's weekly reset


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def event(window: str = "weekly", status: str = "ok", utilization: float | None = 0.5,
          resets_at: str | None = WEEK_RESET, paid: c.PaidUsage | None = None) -> c.RateLimit:
    return c.RateLimit(session="cse_1", ts="2026-11-11T12:00:00Z", seq=1, status=status, window=window,
                       resets_at=resets_at, utilization=utilization, paid=paid)


def advisory(clock: Clock, **kwargs) -> Advisory:
    return Advisory(clock=clock, **kwargs)


class UsageRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.advisory = advisory(self.clock)

    def window(self, name: str = "weekly"):
        return self.advisory.snapshot().account(*ACCOUNT).windows[name]

    def test_a_reported_window_carries_its_source_age_and_minute(self) -> None:
        self.advisory.record_usage(*ACCOUNT, event(utilization=0.04, resets_at="2026-11-16T06:52:23Z"))
        window = self.window()
        self.assertEqual((window.utilization.value, window.utilization.source, window.utilization.observed_at),
                         (0.04, "reported", "2026-11-11T12:26:40Z"))
        self.assertEqual(window.resets_at.value, "2026-11-16T06:52:00Z")
        self.assertTrue(window.status.known)

    def test_the_one_second_jitter_is_one_window(self) -> None:
        self.advisory.record_usage(*ACCOUNT, event(utilization=0.98, resets_at="2026-11-16T06:52:23Z"))
        self.advisory.record_usage(*ACCOUNT, event(utilization=0.99, resets_at="2026-11-16T06:52:24Z"))
        self.assertEqual((self.window().utilization.value, self.window().resets_at.value),
                         (0.99, "2026-11-16T06:52:00Z"))

    def test_jitter_across_a_minute_boundary_is_still_one_window(self) -> None:
        # 06:52:30 and 06:52:31 round to different minutes: still one window, so the flap stays out.
        self.advisory.record_usage(*ACCOUNT, event(status="limited", utilization=1.0, resets_at="2026-11-16T06:52:30Z"))
        self.clock.now += 3
        self.advisory.record_usage(*ACCOUNT, event(status="warning", utilization=0.99,
                                                   resets_at="2026-11-16T06:52:31Z"))
        self.advisory.record_usage(*ACCOUNT, event(status="limited", utilization=1.0, resets_at="2026-11-16T06:52:30Z"))
        window = self.window()
        self.assertEqual((window.utilization.value, window.status.value, window.resets_at.value),
                         (1.0, "limited", "2026-11-16T06:52:00Z"))

    def test_the_vendors_measured_read_may_lower_a_window(self) -> None:
        # Credits bought mid-window: the vendor now says ok, paid usage in use.
        self.advisory.record_usage(*ACCOUNT, event(status="limited", utilization=1.0))
        self.clock.now += 60
        read = c.UsageSnapshot(windows=(c.UsageWindow(name="weekly", status="ok", utilization=1.0,
                                                      resets_at=WEEK_RESET),),
                               account=ACCOUNT[1], paid=c.PaidUsage(in_use=True), source="measured")
        self.advisory._book.record_read(ACCOUNT, read, self.clock.now)
        self.advisory._publish()
        window = self.window()
        self.assertEqual((window.status.value, window.status.source), ("ok", "measured"))
        self.assertTrue(self.advisory.snapshot().account(*ACCOUNT).paid.in_use.value)

    def test_a_measured_read_with_no_status_keeps_the_status_turns_reported(self) -> None:
        self.advisory.record_usage(*ACCOUNT, event(status="warning", utilization=0.9))
        read = c.UsageSnapshot(windows=(c.UsageWindow(name="weekly", status=None, utilization=0.93,
                                                      resets_at=WEEK_RESET),), account=ACCOUNT[1], source="measured")
        self.advisory._book.record_read(ACCOUNT, read, self.clock.now + 30)
        self.clock.now += 30
        self.advisory._publish()
        window = self.window()
        self.assertEqual((window.utilization.value, window.utilization.source), (0.93, "measured"))
        self.assertEqual((window.status.value, window.status.source), ("warning", "reported"))

    def test_a_window_ends_at_its_exact_reset(self) -> None:
        self.advisory.record_usage(*ACCOUNT, event(utilization=0.6, resets_at="2026-11-11T12:30:20Z"))
        self.clock.now = T0 + 215                           # 12:30:15: published as 12:30, not over yet
        self.advisory._publish()
        self.assertEqual((self.window().utilization.value, self.window().resets_at.value),
                         (0.6, "2026-11-11T12:30:00Z"))
        self.clock.now = T0 + 221                           # 12:30:21
        self.advisory._publish()
        self.assertFalse(self.window().utilization.known)

    def test_a_lower_report_for_the_same_window_never_lowers_it(self) -> None:
        # The flap: 100 % from one process, then 99 % from a call made just before it.
        self.advisory.record_usage(*ACCOUNT, event(status="limited", utilization=1.0))
        self.clock.now += 3
        self.advisory.record_usage(*ACCOUNT, event(status="warning", utilization=0.99))
        window = self.window()
        self.assertEqual((window.utilization.value, window.status.value), (1.0, "limited"))
        self.assertEqual((window.utilization.source, window.utilization.observed_at),
                         ("derived", "2026-11-11T12:26:43Z"))      # confirmed by the later reading

    def test_a_report_with_no_number_keeps_the_last(self) -> None:
        self.advisory.record_usage(*ACCOUNT, event(utilization=0.42))
        self.advisory.record_usage(*ACCOUNT, event(status="warning", utilization=None))
        self.assertEqual((self.window().utilization.value, self.window().status.value), (0.42, "warning"))

    def test_an_earlier_window_from_a_late_process_is_dropped_but_the_vendors_word_is_taken(self) -> None:
        self.advisory.record_usage(*ACCOUNT, event(utilization=0.1, resets_at="2026-11-16T06:52:00Z"))
        self.advisory.record_usage(*ACCOUNT, event(utilization=0.9, resets_at="2026-11-09T06:52:00Z"))
        self.assertEqual(self.window().utilization.value, 0.1)
        read = c.UsageSnapshot(windows=(c.UsageWindow(name="weekly", utilization=0.3,
                                                      resets_at="2026-11-15T06:52:00Z"),),
                               account="acct-4d3aa4c0d340", source="measured")
        self.advisory._book.record_read(ACCOUNT, read, self.clock.now)
        self.advisory._publish()
        self.assertEqual((self.window().utilization.value, self.window().resets_at.value),
                         (0.3, "2026-11-15T06:52:00Z"))

    def test_a_reset_is_never_assumed_and_the_next_window_starts_fresh(self) -> None:
        self.advisory.record_usage(*ACCOUNT, event(status="limited", utilization=1.0, resets_at="2026-11-11T12:30:00Z"))
        self.clock.now = T0 + 240                           # past 12:30
        self.advisory._publish()
        window = self.window()
        self.assertEqual((window.utilization.known, window.status.known, window.resets_at.known),
                         (False, False, False))
        self.assertEqual(window.utilization.observed_at, "2026-11-11T12:26:40Z")   # when it was last seen
        self.advisory.record_usage(*ACCOUNT, event(utilization=0.02, resets_at="2026-11-18T12:30:00Z"))
        self.assertEqual((self.window().utilization.value, self.window().status.value), (0.02, "ok"))

    def test_a_stale_fact_shows_as_unknown_and_announces_itself(self) -> None:
        heard: list[frozenset] = []
        self.advisory.record_usage(*ACCOUNT, event(utilization=0.5, paid=c.PaidUsage(possible=True, credits=7187.0)))
        self.advisory.on_change(lambda snapshot, changed: heard.append(changed))
        self.clock.now += 16 * 60
        self.advisory._publish()
        account = self.advisory.snapshot().account(*ACCOUNT)
        self.assertFalse(account.windows["weekly"].utilization.known)
        self.assertIsNone(account.windows["weekly"].utilization.value)
        self.assertEqual(account.paid.credits.value, 7187.0)                # paid facts last an hour
        self.assertEqual(heard, [frozenset({ACCOUNT})])

    def test_paid_facts_keep_their_last_value_until_the_vendor_says_another(self) -> None:
        self.advisory.record_usage(*ACCOUNT, event(paid=c.PaidUsage(possible=False, status="rejected",
                                                                disabled_reason="org_level_disabled")))
        self.advisory.record_usage(*ACCOUNT, event(paid=c.PaidUsage(in_use=False)))
        paid = self.advisory.snapshot().account(*ACCOUNT).paid
        self.assertEqual((paid.possible.value, paid.in_use.value, paid.status.value, paid.credits.known),
                         (False, False, "rejected", False))

    def test_an_account_is_its_agent_and_label_and_none_is_an_account_of_its_own(self) -> None:
        self.advisory.record_usage("codex", None, event(utilization=0.2))      # the runtime named no account
        self.advisory.record_usage("codex", "default", event(utilization=0.7))
        self.advisory.record_usage(*ACCOUNT, event(utilization=0.5))
        found = self.advisory.snapshot()
        self.assertEqual(found.account("codex", None).windows["weekly"].utilization.value, 0.2)
        self.assertEqual(found.account("codex", "default").windows["weekly"].utilization.value, 0.7)
        self.assertEqual((found.account(*ACCOUNT).agent, found.account(*ACCOUNT).account), ACCOUNT)
        self.assertIsNone(found.account("claude_code", None))
        self.assertEqual(set(found.accounts), {("codex", None), ("codex", "default"), ACCOUNT})


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_versions_rise_only_when_a_fact_changes(self) -> None:
        clock = Clock()
        found = advisory(clock)
        found.record_usage(*ACCOUNT, event(utilization=0.5))
        first = found.snapshot()
        found.record_usage(*ACCOUNT, event(utilization=0.5))         # the same, at the same second
        self.assertIs(found.snapshot(), first)
        clock.now += 1
        found.record_usage(*ACCOUNT, event(utilization=0.6))
        self.assertEqual(found.snapshot().version, first.version + 1)

    async def test_wait_newer_returns_at_the_next_version_or_its_timeout(self) -> None:
        clock = Clock()
        found = advisory(clock)
        waiting = asyncio.create_task(found.wait_newer(0, 5))
        await asyncio.sleep(0)
        found.record_usage(*ACCOUNT, event())
        self.assertEqual((await waiting).version, 1)
        self.assertEqual((await found.wait_newer(1, 0.01)).version, 1)

    async def test_the_engines_calls_never_raise(self) -> None:
        found = advisory(Clock())

        def broken(snapshot, changed) -> None:
            raise RuntimeError("a listener's bug")
        found.on_change(broken)
        with self.assertLogs("services.advisory.advisory", "ERROR"):
            found.record_usage(*ACCOUNT, event())                   # the listener fails, the caller goes on
        found._book = None                                      # the advisory itself broken
        with self.assertLogs("services.advisory.advisory", "ERROR"):
            found.record_usage(*ACCOUNT, event())
            found.record_turn("cse_1", c.TokenUsage(cost_usd=0.1), agent="codex", account=None)

    async def test_a_sessions_turn_and_its_end(self) -> None:
        found = advisory(Clock())
        found.record_turn("cse_1", c.TokenUsage(cost_usd=0.15, context_tokens=141362, context_window=1_000_000),
                          agent="codex", account="acct-4d3aa4c0d340")
        session = found.snapshot().sessions["cse_1"]
        self.assertEqual(((session.agent, session.account), session.turn_cost_usd.value, session.context_tokens.value,
                          session.context_window.value), (ACCOUNT, 0.15, 141362, 1_000_000))
        found.track_process("cse_1", 4242, 77, agent="codex", account="acct-4d3aa4c0d340")
        self.assertEqual(found._sessions["cse_1"].root, Root(4242, 77))
        found.forget_session("cse_1")
        self.assertNotIn("cse_1", found.snapshot().sessions)

    async def test_the_snapshot_serialises_with_ages(self) -> None:
        clock = Clock()
        found = advisory(clock)
        found.record_usage(*ACCOUNT, event(utilization=0.5))
        found.record_usage("codex", None, event(utilization=0.1))
        body = found.snapshot().as_dict(T0 + 30)
        unnamed, named = body["accounts"]                       # a list: no key format leaves the advisory
        utilization = named["windows"]["weekly"]["utilization"]
        self.assertEqual(utilization, {"value": 0.5, "source": "reported", "observed_at": "2026-11-11T12:26:40Z",
                                       "age_s": 30.0, "known": True})
        self.assertEqual([(a["agent"], a["account"]) for a in (unnamed, named)], [("codex", None), ACCOUNT])


CGROUP = {
    "cpu.max": "200000 100000\n",
    "cpu.stat": "usage_usec 101883800518\nuser_usec 81221504856\nsystem_usec 20662295661\n",
    "cpu.pressure": "some avg10=0.01 avg60=0.24 avg300=0.25 total=2398842413\n"
                    "full avg10=0.01 avg60=0.24 avg300=0.25 total=2327539812\n",
    "memory.max": "8589934592\n",
    "memory.current": "7993954304\n",
    "memory.stat": "anon 3372957696\nfile 3126505472\ninactive_file 2609401856\nactive_file 517148672\n",
    "memory.events": "low 0\nhigh 0\nmax 2\noom 1\noom_kill 1\noom_group_kill 0\n",
    "memory.pressure": "some avg10=1.50 avg60=0.00 avg300=0.00 total=17706064\n"
                       "full avg10=0.75 avg60=0.00 avg300=0.00 total=17414876\n",
    "io.pressure": "some avg10=0.00 avg60=0.05 avg300=0.74 total=241468888\n",
}


class MachineTests(unittest.TestCase):
    def test_the_containers_limits_and_working_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for name, text in CGROUP.items():
                (folder / name).write_text(text)
            first = read_sample(T0, folder, monotonic=lambda: 500.0)
            (folder / "cpu.stat").write_text("usage_usec 101885800518\n")      # 2 s of CPU time in 2 s
            second = read_sample(T0 + 3, folder, monotonic=lambda: 502.0)    # asked for 3 s on, read 2 s on
        facts = machine(second, first, T0 + 3)
        self.assertEqual((facts.cpu_limit.value, facts.cpu_used.value, facts.cpu_used.source), (2.0, 1.0, "derived"))
        self.assertEqual((facts.memory_limit.value, facts.memory_working_set.value, facts.memory_available.value),
                         (8589934592, 7993954304 - 2609401856, 8589934592 - (7993954304 - 2609401856)))
        self.assertEqual((facts.memory_pressure.value, facts.memory_pressure_full.value, facts.oom_kills.value,
                          facts.memory_limit_hits.value), (1.5, 0.75, 1, 2))
        self.assertFalse(machine(second, first, T0 + 14).memory_limit.known)  # the sampler stopped: unknown
        # Served later than published: a fact past its age is unknown when served, too.
        self.assertTrue(facts.memory_limit.known)
        self.assertEqual(facts.as_dict(T0 + 14)["memory_limit"]["known"], False)
        self.assertEqual(facts.as_dict(T0 + 4)["memory_limit"]["value"], 8589934592)

    def test_unreadable_files_leave_their_facts_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            facts = machine(read_sample(T0, Path(tmp), proc=Path(tmp)), None, T0)
        self.assertFalse(facts.memory_working_set.known)
        self.assertFalse(facts.cpu_used.known)

    def test_parsers(self) -> None:
        self.assertIsNone(parse_cpu_max("max 100000"))
        self.assertEqual(parse_cpu_max("150000 100000"), 1.5)
        self.assertEqual(parse_pressure("some avg10=2.5 avg60=0 total=1\n\nfull avg10=x\n"), {"some": 2.5})
        with tempfile.TemporaryDirectory() as tmp:
            proc, root = Path(tmp) / "proc", Path(tmp) / "cg"
            (proc / "self").mkdir(parents=True)
            (root / "user.slice").mkdir(parents=True)
            (root / "user.slice" / "memory.current").write_text("1\n")
            (proc / "self" / "cgroup").write_text("0::/user.slice\n")
            self.assertEqual(cgroup_dir(root, proc), root / "user.slice")
            (proc / "self" / "cgroup").write_text("0::/elsewhere\n")
            self.assertEqual(cgroup_dir(root, proc), root)


def fake_proc(folder: Path, processes: dict[int, tuple[int, int, int | None]]) -> Path:
    """pid -> (ppid, start ticks, PSS kB or None for unreadable)."""
    for pid, (ppid, ticks, pss) in processes.items():
        (folder / str(pid)).mkdir()
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(ticks)]
        (folder / str(pid) / "stat").write_text(f"{pid} (claude) {' '.join(fields)}\n")
        if pss is not None:
            (folder / str(pid) / "smaps_rollup").write_text(f"Rss: 9 kB\nPss: {pss} kB\n")
    return folder


class SessionTests(unittest.TestCase):
    def test_a_sessions_tree_is_measured_by_pss_and_a_reused_pid_is_not_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = fake_proc(Path(tmp), {1: (0, 1, None), 100: (1, 500, 1000), 101: (100, 510, 200),
                                         102: (101, 520, 50), 103: (100, 530, None), 200: (1, 600, 7)})
            found = measure({"cse_a": Root(100, 500), "cse_b": Root(200, 999), "cse_c": Root(300, None)}, proc)
        self.assertEqual((found["cse_a"].pss, found["cse_a"].processes), (1250 * 1024, 4))
        self.assertEqual((found["cse_b"].pss, found["cse_b"].processes), (None, 0))   # pid 200 is another now
        self.assertEqual((found["cse_c"].pss, found["cse_c"].processes), (None, 0))


class Reader:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls = 0

    async def read_usage(self) -> c.UsageSnapshot:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def read(utilization: float, *, account: str | None = "acct-70c53cc54fd1", status: str = "ok",
         resets_at: str = "2026-11-12T01:40:00Z", in_use: bool | None = None) -> c.UsageSnapshot:
    return c.UsageSnapshot(windows=(c.UsageWindow(name="five_hour", status=status, utilization=utilization,
                                                  resets_at=resets_at),),
                           account=account, paid=c.PaidUsage(possible=False, in_use=in_use), source="measured")


async def settle() -> None:
    """Let the read loop run: it lists the readers in a thread."""
    for _ in range(5):
        await asyncio.sleep(0.02)


class ReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_read_is_measured_and_paced_by_the_vendors_own_words_only(self) -> None:
        clock = Clock()
        reader = Reader(read(0.51), read(0.97), read(0.97, status="warning"),
                        read(0.4, in_use=True, resets_at="2026-11-12T06:40:00Z"))
        found = advisory(clock, readers=lambda: {"claude_code": reader})
        await found._read("claude_code", reader)
        window = found.snapshot().account("claude_code", "acct-70c53cc54fd1").windows["five_hour"]
        self.assertEqual((window.utilization.value, window.utilization.source), (0.51, "measured"))
        self.assertEqual(found._next_read["claude_code"], T0 + READ_CALM_S)
        await found._read("claude_code", reader)                 # 97 % but the vendor says ok: no threshold here
        self.assertEqual(found._next_read["claude_code"], T0 + READ_CALM_S)
        await found._read("claude_code", reader)                 # the vendor's warning
        self.assertEqual(found._next_read["claude_code"], T0 + READ_NEAR_S)
        clock.now = T0 + 3 * 3600                                # the next window, on paid usage
        await found._read("claude_code", reader)
        self.assertEqual(found._next_read["claude_code"], clock.now + READ_NEAR_S)

    async def test_a_read_comes_again_just_after_a_reset(self) -> None:
        clock = Clock()
        reader = Reader(read(0.2, resets_at="2026-11-11T12:28:00Z"), read(0.2, resets_at="2026-11-11T12:30:20Z"))
        found = advisory(clock, readers=lambda: {"claude_code": reader})
        await found._read("claude_code", reader)
        self.assertEqual(found._next_read["claude_code"], T0 + 80 + 5)
        clock.now = T0 + 90
        await found._read("claude_code", reader)                 # its exact reset, not the minute it rounds to
        self.assertEqual(found._next_read["claude_code"], T0 + 220 + 5)

    async def test_a_failed_read_waits_and_is_logged_once(self) -> None:
        clock = Clock()
        expired = c.RuntimeFailure("auth_required", "claude's access token has expired")
        reader = Reader(expired, expired, read(0.3))
        found = advisory(clock, readers=lambda: {"claude_code": reader})
        with self.assertLogs("services.advisory.advisory", "INFO") as logs:
            await found._read("claude_code", reader)
            await found._read("claude_code", reader)
            await found._read("claude_code", reader)
        self.assertEqual(len([r for r in logs.output if "can't be read" in r]), 1)
        self.assertEqual(len([r for r in logs.output if "reads again" in r]), 1)
        self.assertEqual(found.snapshot().version, 1)

    async def test_the_loop_reads_each_runtime_and_a_request_comes_at_most_once_a_minute(self) -> None:
        clock = Clock()
        reader = Reader(read(0.3), read(0.31))
        found = advisory(clock, readers=lambda: {"claude_code": reader})
        loop = asyncio.create_task(found._read_loop())
        try:
            await settle()
            self.assertEqual(reader.calls, 1)
            found.request_read("claude_code")                     # read just now: not again
            await settle()
            self.assertEqual(reader.calls, 1)
            clock.now += 61
            found.request_read("claude_code")
            await settle()
            self.assertEqual(reader.calls, 2)
        finally:
            loop.cancel()
            await asyncio.gather(loop, return_exceptions=True)

    async def test_a_read_with_no_account_is_kept_by_no_one(self) -> None:
        reader = Reader(read(0.3, account=None))
        found = advisory(Clock(), readers=lambda: {"claude_code": reader})
        await found._read("claude_code", reader)
        self.assertEqual(dict(found.snapshot().accounts), {})


if __name__ == "__main__":
    unittest.main()
