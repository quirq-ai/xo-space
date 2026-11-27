"""``kept_check``: a CLI check kept until what it reads changes (the rule
claude_code's ``describe()`` keeps), shared by every adapter whose checks start
a CLI; and the ACP runtimes (Cursor, Grok Build) reading their version, login
and models through it. No real CLI is run."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.cowork_agent.adapters.contract import RuntimeFailure
from services.cowork_agent.adapters.kept_check import RETRY_AFTER_S, KeptCheck, KeptProbe, stamp
from services.cowork_agent.adapters.loader import try_load_capability


class StampTest(unittest.TestCase):
    def test_a_file_written_or_removed_and_a_variable_changed_move_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            missing = stamp([path], {"KEY": ""})
            path.write_text("a")
            written = stamp([path], {"KEY": ""})
            self.assertNotEqual(missing, written)
            self.assertEqual(written, stamp([path], {"KEY": ""}))
            os.utime(path, ns=(1, 1))
            touched = stamp([path], {"KEY": ""})
            self.assertNotEqual(written, touched)
            self.assertNotEqual(touched, stamp([path], {"KEY": "k"}))
            path.unlink()
            self.assertEqual(missing, stamp([path], {"KEY": ""}))

    def test_a_link_repointed_by_an_update_moves_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old, new, link = Path(tmp) / "v1", Path(tmp) / "v2", Path(tmp) / "cli"
            old.write_text("1")
            new.write_text("2")
            link.symlink_to(old)
            before = stamp([link])
            link.unlink()
            link.symlink_to(new)
            self.assertNotEqual(before, stamp([link]))

    def test_the_key_holds_no_variable_value(self) -> None:
        self.assertNotIn("secret-value", repr(stamp([], {"KEY": "secret-value"})))


class KeptCheckTest(unittest.TestCase):
    def test_an_answer_is_kept_per_key_until_forgotten(self) -> None:
        calls = []
        kept = KeptCheck[str](lambda answer: answer != "")

        def check(answer):
            return lambda: calls.append(answer) or answer
        self.assertEqual(kept.get("a", check("one")), "one")
        self.assertEqual(kept.get("a", check("two")), "one")
        self.assertEqual(kept.get("b", check("two")), "two")
        kept.forget()
        self.assertEqual(kept.get("b", check("three")), "three")
        self.assertEqual(calls, ["one", "two", "three"])

    def test_an_outcome_that_isnt_an_answer_is_kept_only_a_while(self) -> None:
        now, calls = [0.0], []
        kept = KeptCheck[str](lambda answer: answer != "", clock=lambda: now[0])
        check = lambda: calls.append(1) or ""
        kept.get("k", check)
        now[0] += RETRY_AFTER_S - 1
        kept.get("k", check)
        self.assertEqual(len(calls), 1)
        now[0] += 2
        kept.get("k", check)
        self.assertEqual(len(calls), 2)
        kept.get("other", check)                # a new key asks at once
        self.assertEqual(len(calls), 3)

    def test_callers_that_arrive_while_it_runs_share_its_outcome(self) -> None:
        kept, calls, answers = KeptCheck[int](bool), [], []

        def slow() -> int:
            calls.append(1)
            time.sleep(0.05)
            return 7
        threads = [threading.Thread(target=lambda: answers.append(kept.get("k", slow))) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((len(calls), answers), (1, [7] * 5))


class ForgetTest(unittest.TestCase):
    """``forget()`` is called on the event loop: it must never wait for a check."""

    def test_forget_returns_at_once_and_a_check_from_before_it_is_not_kept(self) -> None:
        kept, started, release, calls = KeptCheck[str](bool), threading.Event(), threading.Event(), []

        def slow() -> str:
            calls.append(1)
            started.set()
            release.wait(5)
            return "before"
        worker = threading.Thread(target=lambda: kept.get("k", slow))
        worker.start()
        started.wait(5)
        began = time.monotonic()
        kept.forget()
        self.assertLess(time.monotonic() - began, 0.05)
        release.set()
        worker.join()
        self.assertEqual(kept.get("k", lambda: calls.append(1) or "after"), "after")
        self.assertEqual(len(calls), 2)


class KeptProbeTest(unittest.IsolatedAsyncioTestCase):
    """The same rule for a probe that is a coroutine (the claude_code, codex and agy runtimes' probes)."""

    def setUp(self) -> None:
        self.now, self.runs = 0.0, []

    def kept(self, **kw) -> KeptProbe:
        return KeptProbe("the list", clock=lambda: self.now, **kw)

    def probe(self, outcome, *, gate: asyncio.Event | None = None):
        async def run():
            self.runs.append(outcome)
            if gate is not None:
                await gate.wait()
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return run

    async def test_an_answer_is_kept_until_the_key_moves_and_a_failure_only_a_while(self) -> None:
        kept = self.kept(retry_after_s=30)
        self.assertEqual(await kept.get("k", self.probe("a")), "a")
        self.assertEqual(await kept.get("k", self.probe("b")), "a")
        with self.assertRaises(RuntimeFailure):
            await kept.get("k2", self.probe(RuntimeFailure("timeout", "no answer")))
        with self.assertRaises(RuntimeFailure) as again:
            await kept.get("k2", self.probe("c"))
        self.assertEqual((again.exception.code, again.exception.retryable), ("timeout", True))
        self.assertIn("not asked again yet", again.exception.message)
        self.now = 31
        self.assertEqual(await kept.get("k2", self.probe("c")), "c")
        self.assertEqual(self.runs, ["a", self.runs[1], "c"])

    async def test_no_retry_wait_keeps_no_failure_and_fresh_skips_only_an_answer(self) -> None:
        kept = self.kept(retry_after_s=0)
        with self.assertRaises(RuntimeFailure):
            await kept.get("k", self.probe(RuntimeFailure("cli_missing", "gone")))
        self.assertEqual(await kept.get("k", self.probe("a")), "a")
        self.assertEqual(await kept.get("k", self.probe("b"), fresh=True), "b")
        timed = self.kept(retry_after_s=30, answer_for_s=300)
        await timed.get("k", self.probe("x"))
        self.now = 299
        self.assertEqual(await timed.get("k", self.probe("y")), "x")
        self.now = 300
        self.assertEqual(await timed.get("k", self.probe("y")), "y")

    async def test_callers_share_one_probe_across_threads_and_a_late_waiter_gives_up_alone(self) -> None:
        kept, gate = self.kept(wait_s=0.1), asyncio.Event()
        first = asyncio.ensure_future(kept.get("k", self.probe("a", gate=gate)))
        await asyncio.sleep(0)
        with self.assertRaises(RuntimeFailure) as late:
            await kept.get("k", self.probe("b"))
        self.assertIn("the list is still being read", late.exception.message)
        loop = asyncio.get_running_loop()
        other = loop.run_in_executor(None, lambda: asyncio.run(kept.get("k", self.probe("c"))))
        await asyncio.sleep(0.05)
        gate.set()
        self.assertEqual((await first, await other), ("a", "a"))
        self.assertEqual(self.runs, ["a"])

    async def test_a_cancelled_probe_keeps_nothing_and_forget_stops_sharing(self) -> None:
        kept, gate = self.kept(), asyncio.Event()
        first = asyncio.ensure_future(kept.get("k", self.probe("a", gate=gate)))
        await asyncio.sleep(0)
        waiter = asyncio.ensure_future(kept.get("k", self.probe("b")))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(RuntimeFailure) as told:
            await waiter
        self.assertIn("was cancelled", told.exception.message)
        self.assertEqual(await kept.get("k", self.probe("c")), "c")
        gate2 = asyncio.Event()
        running = asyncio.ensure_future(kept.get("j", self.probe("old", gate=gate2)))
        await asyncio.sleep(0)
        kept.forget()
        self.assertEqual(await kept.get("j", self.probe("new")), "new")
        gate2.set()
        self.assertEqual(await running, "old")
        self.assertEqual(await kept.get("j", self.probe("newer")), "new")


class AcpRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """Cursor and Grok Build start their CLI once for each thing they are
    asked, until the binary or the login changes."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home / "config"),
                                               "CURSOR_CONFIG_DIR": str(self.home / ".cursor"),
                                               "GROK_HOME": str(self.home / ".grok")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def runtime(self, agent: str, outputs: dict[str, str]):
        """A fresh runtime of ``agent`` over a fake binary; ``outputs`` by first argument. Its runs, listed."""
        module = try_load_capability("runtime", agent=agent)
        runtime = type(module.runtime())()
        lifecycle = runtime.lifecycle
        binary = self.home / "bin" / agent
        binary.parent.mkdir(exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        runs: list[tuple[str, ...]] = []

        def run_sync(argv, **kwargs):
            runs.append(tuple(argv[1:]))
            return SimpleNamespace(ok=True, output=outputs[argv[1]], stderr="", timed_out=False)
        for patcher in (mock.patch("services.cowork_agent.acp.runtime.run_sync", run_sync),
                        mock.patch.object(lifecycle, "run_sync", run_sync),
                        mock.patch.object(lifecycle, "find_binary", lambda: str(binary)),
                        mock.patch.object(type(runtime), "binary", lambda self: str(binary))):
            patcher.start()
            self.addCleanup(patcher.stop)
        lifecycle.forget_login()
        self.addCleanup(lifecycle.forget_login)
        return runtime, runs, binary

    async def test_cursor_describes_and_lists_its_models_once_until_its_login_changes(self) -> None:
        runtime, runs, _ = self.runtime("cursor", {"--version": "2026.11.22\n", "status": '{"isAuthenticated": true}',
                                                   "--list-models": "auto - Auto\ngpt-5 - GPT-5\n"})
        for _ in range(3):
            info = await runtime.describe()
            models = await runtime.models()
        self.assertEqual((info.cli_version, info.logged_in, [m.id for m in models]),
                         ("2026.11.22", True, ["auto", "gpt-5"]))
        self.assertEqual(sorted(runs), [("--list-models",), ("--version",), ("status", "--format", "json")])
        auth = self.home / "config" / "cursor" / "auth.json"
        auth.parent.mkdir(parents=True)
        auth.write_text("{}")                       # a sign-in: the login and the models are asked again
        await runtime.describe()
        await runtime.models()
        self.assertEqual(len(runs), 5)

    async def test_grok_asks_its_version_again_after_an_update(self) -> None:
        runtime, runs, binary = self.runtime("grok_build", {"--version": "grok 1.0.92\n",
                                                            "models": "Available models:\n* grok-4\n"})
        self.assertEqual((await runtime.describe()).cli_version, "grok 1.0.92")
        await runtime.describe()
        self.assertEqual([m.id for m in await runtime.models()], ["grok-4"])
        self.assertEqual(runs, [("--version",), ("models",)])     # its login's run lists its models too
        binary.write_text("#!/bin/sh\n# updated\n")
        await runtime.describe()
        self.assertEqual(runs.count(("--version",)), 2)

    async def test_a_runtime_without_a_lifecycle_says_so(self) -> None:
        from services.cowork_agent.acp.runtime import AcpRuntime
        with self.assertRaisesRegex(NotImplementedError, "lifecycle"):
            await AcpRuntime().logged_in()


if __name__ == "__main__":
    unittest.main()
