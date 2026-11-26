"""Runtimes into storm (plan §5.5, §5.6): each runtime's CLI as an advisory
fact, read locally or on request; a change recorded as ``runtimes.changed``,
recycling the project coordinators it concerns and told at their next turn;
aliases; the first signed-in runtime as the default."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services import advisory as space_advisory
from services.advisory import RuntimeFacts, Snapshot
from services.advisory.advisory import Advisory
from services.advisory.model import frozen
from services.advisory.runtimes import DescribedCli, LocalCli, _versions
from services.cowork_agent.adapters.contract import RuntimeInfo
from services.cowork_agent.registry import agent_registry as reg
from services.cowork_agent.storm import notices, runtime_policy
from services.cowork_agent.storm.chats.digest import SinceLastTurn
from services.cowork_agent.storm.chats.router import write_setting
from services.runtime_lifecycle.contract import LifecycleSpec
from tests.storm_comms_kit import CommsTestCase

T0 = 1_794_400_000.0


class Reader:
    def __init__(self, facts: RuntimeFacts | None, polled: bool) -> None:
        self.facts, self.polled, self.reads = facts, polled, 0

    async def read(self) -> RuntimeFacts | None:
        self.reads += 1
        return self.facts


class AdvisoryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unpolled_and_unread_runtimes_are_read_on_request_and_since_stays_while_unchanged(self):
        clock = [T0]
        local = Reader(RuntimeFacts("local", True, True, "acct"), polled=True)
        asked = Reader(RuntimeFacts("asked", True, False), polled=False)
        advisory = Advisory(clock=lambda: clock[0], clis=lambda: {"local": local, "asked": asked})
        heard = []
        advisory.on_change(lambda snapshot, _accounts: heard.append(snapshot.version))

        first = await advisory.read_runtimes(1.0)
        self.assertEqual((first["local"].signed_in, first["asked"].installed), (True, True))
        self.assertEqual((local.reads, asked.reads, len(heard)), (1, 1, 1))
        since = first["asked"].since

        clock[0] += 60
        again = await advisory.read_runtimes(1.0)
        self.assertEqual((local.reads, asked.reads, len(heard)), (1, 2, 1))     # polled: left to its loop
        self.assertEqual(again["asked"].since, since)                          # nothing new: no new version

        asked.facts = RuntimeFacts("asked", True, True, "who")
        changed = await advisory.read_runtimes(1.0)
        self.assertEqual((changed["asked"].signed_in, changed["asked"].account, len(heard)), (True, "who", 2))
        self.assertNotEqual(changed["asked"].since, since)
        self.assertIn("runtimes", advisory.snapshot().as_dict(clock[0]))

    async def test_a_polled_drop_is_published_only_when_the_next_read_agrees(self):
        reader = Reader(RuntimeFacts("x", True, True), polled=True)
        advisory = Advisory(clock=lambda: T0, clis=lambda: {"x": reader})

        async def poll(at_once: frozenset = frozenset()) -> bool:
            await advisory._read_clis({"x": reader}, 1.0, at_once=set(at_once))
            return advisory.snapshot().runtimes["x"].signed_in

        self.assertTrue(await poll())
        reader.facts = RuntimeFacts("x", True, False)
        self.assertTrue(await poll())                   # a token file caught mid-rewrite
        reader.facts = RuntimeFacts("x", True, True)
        self.assertTrue(await poll())                   # it was: nothing published
        reader.facts = RuntimeFacts("x", True, False)
        self.assertTrue(await poll())
        self.assertFalse(await poll())                  # confirmed
        reader.facts = RuntimeFacts("x", True, True)
        self.assertTrue(await poll())                   # back in: at once
        reader.facts = RuntimeFacts("x", True, False)
        self.assertFalse(await poll(frozenset({"x"})))  # an action through XO: at once

    async def test_a_reader_that_fails_keeps_what_was_known(self):
        reader = Reader(RuntimeFacts("x", True, True), polled=False)
        advisory = Advisory(clock=lambda: T0, clis=lambda: {"x": reader})
        await advisory.read_runtimes(1.0)
        reader.facts = None
        self.assertTrue((await advisory.read_runtimes(1.0))["x"].signed_in)


class LocalCliTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.binary = Path(tmp.name) / "fakecli"
        self.calls = Path(tmp.name) / "calls"
        self.binary.write_text(f"#!/bin/sh\necho x >> {self.calls}\necho 'fakecli 1.2.3'\n")
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IEXEC)
        self.signed_in = False
        self.addCleanup(_versions.clear)

    def cli(self) -> LocalCli:
        return LocalCli("fake", LifecycleSpec(
            title="Fake", binary=lambda: str(self.binary), logged_in=lambda: self.signed_in,
            account=lambda: "fake:acct", via_api_key=lambda: True, update_note=lambda v: f"update {v}" if v == "1.2.3" else None))

    async def test_reads_the_login_every_time_and_the_version_once_per_binary(self):
        out = await self.cli().read()
        self.assertEqual((out.installed, out.signed_in, out.account, out.version, out.update, out.via_api_key),
                         (True, False, None, "1.2.3", "update 1.2.3", False))
        self.signed_in = True
        out = await self.cli().read()
        self.assertEqual((out.signed_in, out.account, out.via_api_key), (True, "fake:acct", True))
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    async def test_a_changed_binary_is_read_again_and_a_failed_read_is_not_kept(self):
        await self.cli().read()
        stamp = self.binary.stat().st_mtime + 10
        os.utime(self.binary, (stamp, stamp))
        await self.cli().read()
        self.assertEqual(len(self.calls.read_text().splitlines()), 2)
        self.binary.write_text(f"#!/bin/sh\necho x >> {self.calls}\nexit 1\n")
        for _ in range(2):
            self.assertIsNone((await self.cli().read()).version)
        self.assertEqual(len(self.calls.read_text().splitlines()), 4)

    async def test_a_missing_binary_is_not_installed(self):
        os.unlink(self.binary)
        out = await self.cli().read()
        self.assertEqual((out.installed, out.signed_in, out.version), (False, False, None))


class DescribedCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_login_by_api_key_is_kept_only_while_signed_in(self):
        for logged_in in (True, False):
            info = RuntimeInfo(cli_path="/bin/x", cli_version="1", logged_in=logged_in, via_api_key=True)
            out = await DescribedCli("x", SimpleNamespace(describe=mock.AsyncMock(return_value=info))).read()
            self.assertEqual((out.signed_in, out.via_api_key), (logged_in, logged_in))


class LineTests(unittest.TestCase):
    def test_only_a_change_to_what_create_task_offers_is_told(self):
        off, inst, on = ({"installed": i, "signed_in": s, "account": None} for i, s in
                         ((False, False), (True, False), (True, True)))
        self.assertEqual(notices.runtime_line("antigravity", inst, on, "T"),
                         "Runtime antigravity signed in at T; create_task now offers it.")
        self.assertEqual(notices.runtime_line("x", off, on, "T"),
                         "Runtime x was installed, signed in at T; create_task now offers it.")
        self.assertEqual(notices.runtime_line("x", on, inst, "T"),
                         "Runtime x signed out at T; create_task no longer offers it.")
        self.assertEqual(notices.runtime_line("x", on, off, "T"),
                         "Runtime x was uninstalled at T; create_task no longer offers it.")
        self.assertIsNone(notices.runtime_line("x", off, inst, "T"))
        self.assertIsNone(notices.runtime_line("x", on, {**on, "account": "other"}, "T"))


class AliasAndDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(reg.set_preferred_runtime, None)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in ("AGENT_NAME", "DEFAULT_AGENT"):
            os.environ.pop(var, None)

    def test_an_alias_resolves_to_its_runtime_and_the_policy_stores_the_runtime(self):
        self.assertEqual((reg.resolve_runtime("agy"), reg.resolve_runtime("codex"), reg.resolve_runtime("nope")),
                         ("antigravity", "codex", "nope"))
        stored = runtime_policy.validate({"allowed": ["agy", "antigravity", "codex"], "default": "agy"},
                                         ["antigravity", "codex"])
        self.assertEqual(stored, {"allowed": ["antigravity", "codex"], "default": "antigravity"})

    def test_the_first_runtime_by_rank_that_is_signed_in_or_not_read_yet_and_installed(self):
        first, second, third = [m.name for m in reg.runtimes()][:3]
        everything = [m.name for m in reg.runtimes()]
        cases = (({first: True, third: True}, first),                     # two signed in: the rank decides
                 ({third: True}, first),                                  # first not read yet, but installed
                 ({first: False, third: True}, second),                   # second not read yet, but installed
                 ({first: False, second: False, third: True}, third),
                 ({name: False for name in everything}, reg._SAFE_BOOT))
        for signed_in, expected in cases:
            snapshot = Snapshot(1, "", runtimes=frozen({n: RuntimeFacts(n, True, s) for n, s in signed_in.items()}))
            advisory = SimpleNamespace(snapshot=lambda snapshot=snapshot: snapshot)
            with self.subTest(signed_in=signed_in), \
                    mock.patch.object(space_advisory, "current", return_value=advisory), \
                    mock.patch.object(reg.shutil, "which", return_value="/bin/x"):
                self.assertEqual(reg.default_runtime(), expected)


class RuntimeChangeTests(CommsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project("demo")
        self.pc = await self.coordinator(self.chat)

    def snapshot(self, version: int, **runtimes: RuntimeFacts) -> Snapshot:
        return Snapshot(version, f"2026-11-26T12:00:0{version}Z", runtimes=frozen(runtimes))

    async def observe(self, snapshot: Snapshot) -> None:
        task = self.chats.runtime_changes.observe(snapshot)
        if task is not None:
            await task

    def changes(self) -> list[dict]:
        return [json.loads(d) for (d,) in self.rows("SELECT data FROM events WHERE type = 'runtimes.changed' "
                                                     "ORDER BY seq")]

    async def note(self) -> str | None:
        meta = await self.kit.engine.meta(self.pc)
        return await SinceLastTurn().note(self.kit.engine, meta, None, "2000-01-01T00:00:00Z")

    async def test_a_sign_in_is_recorded_recycles_the_coordinators_it_concerns_and_is_told_once(self):
        installed = RuntimeFacts("antigravity", True, False)
        await self.observe(self.snapshot(1, antigravity=installed))            # a first reading: no change
        self.assertEqual(self.changes(), [])
        with mock.patch.object(self.kit.engine, "recycle", wraps=self.kit.engine.recycle) as recycle:
            await self.observe(self.snapshot(2, antigravity=RuntimeFacts("antigravity", True, True, "agy:a")))
        [change] = self.changes()
        self.assertEqual((change["runtime"], change["before"]["signed_in"], change["after"]),
                         ("antigravity", False, {"installed": True, "signed_in": True, "account": "agy:a"}))
        concerned = recycle.call_args.args[0]
        coordinator = SimpleNamespace(role="project-coordinator", chat_id=self.chat["id"])
        self.assertTrue(concerned(coordinator))
        self.assertFalse(concerned(SimpleNamespace(role="thread", chat_id=self.chat["id"])))
        self.assertIn("Runtime antigravity signed in at ", await self.note())

        await self.kit.engine.write(lambda w: write_setting(w, runtime_policy.project_key(self.chat["id"]),
                                                            {"allowed": ["claude_code"]}))
        self.assertNotIn("antigravity", await self.note() or "")               # the project doesn't allow it

    async def test_an_account_switch_is_recorded_but_recycles_and_tells_no_coordinator(self):
        await self.observe(self.snapshot(1, codex=RuntimeFacts("codex", True, True, "a")))
        with mock.patch.object(self.kit.engine, "recycle", wraps=self.kit.engine.recycle) as recycle:
            await self.observe(self.snapshot(2, codex=RuntimeFacts("codex", True, True, "b")))
        self.assertEqual([c["recycle"] for c in self.changes()], [[]])
        self.assertFalse(recycle.call_args.args[0](SimpleNamespace(role="project-coordinator",
                                                                   chat_id=self.chat["id"])))
        self.assertNotIn("codex", await self.note() or "")


if __name__ == "__main__":
    unittest.main()
