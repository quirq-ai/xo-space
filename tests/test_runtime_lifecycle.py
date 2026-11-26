"""The runtime lifecycle engine on fake CLIs: device-code sign-in, jobs, state,
and the guard on what ``lifecycle/`` may import (test_modularity keeps it
agent-free)."""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.runtime_lifecycle import jobs, signin
from services.runtime_lifecycle.contract import LifecycleError, LifecycleSpec, SignIn
from services.runtime_lifecycle.registry import Runtime
from services.runtime_lifecycle.state import state

REPO = Path(__file__).resolve().parent.parent
ADAPTERS = REPO / "services/cowork_agent/adapters"

FAKE = """#!/bin/sh
case "$1" in
  --version) echo "fake 1.2.3" ;;
  login) echo "Open https://example.test/device and enter ABCD-EFGH"; sleep 0.3; touch "$MARK"; sleep 30 ;;
  update) echo "updating"; sleep "${SLOW:-0}"; echo "updated" ;;
  fail) echo "boom" >&2; exit 3 ;;
esac
"""


def _url(line: str) -> str | None:
    found = re.search(r"https://\S+", line)
    return found.group(0) if found else None


class Fake(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cli = self.dir / "fake"
        self.cli.write_text(FAKE)
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IEXEC)
        self.mark = self.dir / "signed-in"
        env = mock.patch.dict(os.environ, {"MARK": str(self.mark), "SLOW": "0"})
        env.start()
        self.addCleanup(env.stop)
        for registry in (jobs._running, jobs._jobs, signin._live, signin._finished):
            self.addCleanup(registry.clear)

    def runtime(self, lifecycle: dict | None = None, binary: str | None = None) -> Runtime:
        spec = LifecycleSpec(title="Fake", binary=lambda: binary or str(self.cli), logged_in=self.mark.exists,
                             sign_in=SignIn(style="device_code", argv=("login",), auth_url=_url),
                             update_note=lambda v: "old" if v == "1.2.3" else None)
        return Runtime("fake", spec, {"binary": "fake", "lifecycle": lifecycle or {}})


class SignInTest(Fake):
    async def test_device_code_signs_in_when_the_login_lands(self) -> None:
        rt = self.runtime()
        with mock.patch.object(signin, "POLL_S", 0.05):
            events = [json.loads(f[6:]) for f in [f async for f in signin.stream(rt)] if f.startswith("data: ")]
        self.assertEqual([e["type"] for e in events], ["session", "auth_url", "stdout", "done"])
        self.assertEqual(events[1]["url"], "https://example.test/device")
        self.assertEqual(events[2]["line"], signin.SUCCESS_LINE)
        self.assertNotIn("fake", signin._live)
        self.assertEqual(signin._finished["fake"], (events[0]["session_id"], True))

    async def test_signed_in_already_is_done_at_once(self) -> None:
        self.mark.touch()
        frames = [f async for f in signin.stream(self.runtime())]
        self.assertEqual([json.loads(f[6:])["type"] for f in frames], ["session", "done"])

    async def test_a_missing_cli_is_not_installed(self) -> None:
        frames = [f async for f in signin.stream(self.runtime(binary=str(self.dir / "absent")))]
        self.assertEqual(json.loads(frames[0][6:])["code"], "not_installed")

    async def test_a_device_code_takes_no_paste(self) -> None:
        rt = self.runtime()
        stream = signin.stream(rt)
        session = json.loads((await anext(stream))[6:])["session_id"]
        with self.assertRaises(LifecycleError) as caught:
            await signin.submit(rt, session, "x", start_path="/start")
        self.assertEqual(caught.exception.code, "no_code")
        self.assertEqual(signin.cancel(rt)["session_id"], session)
        await stream.aclose()


class JobTest(Fake):
    async def finish(self, job: jobs.Job) -> list[dict]:
        return [e async for e in job.follow(heartbeat_s=5)]

    async def test_update_streams_its_lines_and_the_versions(self) -> None:
        rt = self.runtime({"update": {"command": ["{binary}", "update"]}})
        events = await self.finish(await jobs.start(rt, "update", by="founder"))
        self.assertEqual(events[0], {"type": "started", "package": "fake", "version": "1.2.3"})
        self.assertEqual([e["line"] for e in events if e["type"] == "log"], ["updating", "updated"])
        self.assertEqual(events[-1], {"type": "done", "returncode": 0, "version_before": "1.2.3",
                                      "version": "1.2.3", "update": "old"})

    async def test_one_job_at_a_time_and_heartbeats_while_quiet(self) -> None:
        os.environ["SLOW"] = "0.4"
        rt = self.runtime({"update": {"command": ["{binary}", "update"]}})
        job = await jobs.start(rt, "update", by="founder")
        with self.assertRaises(LifecycleError) as caught:
            await jobs.start(rt, "update", by="founder")
        self.assertEqual((caught.exception.code, caught.exception.extra), ("job_running", {"job": job.id}))
        self.assertEqual((await state(rt))["state"], "updating")
        events = [e async for e in job.follow(heartbeat_s=0.1)]
        self.assertIn({"type": "heartbeat"}, events)
        self.assertEqual(job.status, "done")

    async def test_a_failure_is_an_error_event_and_the_failed_state(self) -> None:
        rt = self.runtime({"update": {"command": ["{binary}", "fail"]}})
        events = await self.finish(await jobs.start(rt, "update", by="founder"))
        self.assertEqual(events[-1]["message"], "fake update failed (exit code 3)")
        self.assertEqual((await state(rt))["state"], "failed")

    async def test_refusals(self) -> None:
        with self.assertRaises(LifecycleError) as caught:
            await jobs.start(self.runtime(), "update", by="founder")
        self.assertEqual(caught.exception.code, "unsupported")
        absent = self.runtime({"update": {"command": ["{binary}", "update"]}}, binary=str(self.dir / "absent"))
        with self.assertRaises(LifecycleError) as caught:
            await jobs.start(absent, "update", by="founder")
        self.assertEqual(caught.exception.code, "cli_missing")
        present = self.runtime({"install": {"command": ["true"]}})
        with self.assertRaises(LifecycleError) as caught:
            await jobs.start(present, "install", by="founder")
        self.assertEqual(caught.exception.code, "installed")

    async def test_uninstall_refuses_running_sessions_unless_forced(self) -> None:
        rt = self.runtime({"uninstall": {"command": ["true"]}})
        engine = mock.Mock(running=mock.Mock(return_value=["s1"]), halt=mock.AsyncMock())
        with mock.patch("services.cowork_agent.storm.engine.current", return_value=engine):
            with self.assertRaises(LifecycleError) as caught:
                await jobs.start(rt, "uninstall", by="founder")
            self.assertEqual((caught.exception.code, caught.exception.extra), ("runtime_in_use", {"sessions": ["s1"]}))
            engine.halt.assert_not_called()
            await self.finish(await jobs.start(rt, "uninstall", force=True, by="founder"))
        engine.halt.assert_awaited_once_with("s1", reason="runtime_uninstalled", by="founder")


class StateTest(Fake):
    async def test_states_and_actions(self) -> None:
        rt = self.runtime({"update": {"command": ["{binary}", "update"]}})
        self.assertEqual(await state(rt, version="1.2.3"), {
            "name": "fake", "state": "installed", "installed": True, "signed_in": False,
            "actions": ["update", "sign_in"], "version": "1.2.3", "needs_update": True, "update": "old"})
        self.mark.touch()
        self.assertEqual((await state(rt))["state"], "signed_in")
        self.assertEqual((await state(self.runtime(binary=str(self.dir / "absent"))))["state"], "not_installed")

    def test_agy_floor(self) -> None:
        from services.cowork_agent.adapters.antigravity.lifecycle import update_note
        self.assertIn("older than 1.5.15", update_note("1.5.14"))
        self.assertIsNone(update_note("1.5.15"))


def _imports(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


class GuardTest(unittest.TestCase):
    def test_lifecycle_never_imports_runtime(self) -> None:
        for path in ADAPTERS.glob("*/lifecycle/**/*.py"):
            bad = [m for m in _imports(path) if re.search(r"adapters\.\w+\.runtime\b", m)]
            self.assertEqual(bad, [], path)

    def test_only_storm_runtimes_have_a_lifecycle(self) -> None:
        for path in ADAPTERS.glob("*/lifecycle"):
            self.assertTrue((path.parent / "runtime").is_dir(), path)


if __name__ == "__main__":
    unittest.main()
