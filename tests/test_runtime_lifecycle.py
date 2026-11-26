"""The runtime lifecycle engine on fake CLIs: device-code sign-in, jobs, state,
and the guard on what ``lifecycle/`` may import (test_modularity keeps it
agent-free)."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
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
  paste)
    n=$(cat "$MARK.runs" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$MARK.runs"
    echo "Visit https://example.test/auth?run=$n"
    [ "$n" = 1 ] && [ -n "$EXPIRE" ] && exit 1
    read -r code
    case "$code" in good) touch "$MARK"; sleep 30 ;; zero) exit 0 ;; *) exit 1 ;; esac ;;
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
        for patch in (mock.patch.dict(os.environ, {"MARK": str(self.mark), "SLOW": "0", "EXPIRE": ""}),
                      mock.patch.object(signin, "POLL_S", 0.05)):
            patch.start()
            self.addCleanup(patch.stop)
        for registry in (jobs._running, jobs._jobs, signin._live, signin._finished):
            self.addCleanup(registry.clear)

    async def asyncSetUp(self) -> None:
        async def reap() -> None:       # last: the CLIs each test's streams stopped
            await asyncio.gather(*signin._reaping)
        self.addAsyncCleanup(reap)

    def runtime(self, lifecycle: dict | None = None, binary: str | None = None, paste: bool = False) -> Runtime:
        sign_in = SignIn(style="paste_code", argv=("paste",), auth_url=_url) if paste else \
            SignIn(style="device_code", argv=("login",), auth_url=_url)
        spec = LifecycleSpec(title="Fake", binary=lambda: binary or str(self.cli), logged_in=self.mark.exists,
                             sign_in=sign_in,
                             update_note=lambda v: "old" if v == "1.2.3" else None)
        return Runtime("fake", spec, {"binary": "fake", "lifecycle": lifecycle or {}})


async def _next(stream) -> dict:
    """The stream's next event, heartbeats skipped."""
    while not (raw := await anext(stream)).startswith("data: "):
        pass
    return json.loads(raw[6:])


class SignInTest(Fake):
    async def test_device_code_signs_in_when_the_login_lands(self) -> None:
        rt = self.runtime()
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

    async def test_a_sign_in_tells_the_listeners_with_its_credential(self) -> None:
        from services.runtime_lifecycle.contract import Credential
        found, finished = Credential("acc"), []
        rt = self.runtime()
        rt = Runtime("fake", dataclasses.replace(
            rt.spec, credential=lambda: found,
            sign_in=dataclasses.replace(rt.spec.sign_in, finish=lambda: finished.append(1))),
            {"binary": "fake", "account_kind": "openai", "lifecycle": {}})
        with mock.patch.object(signin.events, "changed", mock.AsyncMock()) as changed:
            [f async for f in signin.stream(rt)]
            await asyncio.gather(*signin._reaping)
        self.assertEqual(finished, [1])
        changed.assert_awaited_once_with("fake", signed_in="openai", credential=found)


class PasteCodeTest(Fake):
    async def start(self, rt: Runtime):
        stream = signin.stream(rt)
        async def close() -> None:
            await stream.aclose()
        self.addAsyncCleanup(close)
        session = (await _next(stream))["session_id"]
        return stream, session, (await _next(stream))["url"]

    async def test_a_second_stream_attaches_and_the_first_is_superseded(self) -> None:
        rt = self.runtime(paste=True)
        first, session, url = await self.start(rt)
        second, again, replayed = await self.start(rt)
        self.assertEqual((again, replayed), (session, url))
        self.assertEqual((await _next(first))["message"], signin.SUPERSEDED)
        self.assertEqual(await signin.submit(rt, session, " good ", start_path="/s"),
                         {"ok": True, "message": "Code sent", "session_id": session})
        self.assertEqual((await _next(second))["line"], signin.SUCCESS_LINE)
        self.assertEqual((await _next(second))["type"], "done")
        self.assertEqual((await signin.submit(rt, session, "good", start_path="/s"))["message"],
                         "Login already completed")

    async def test_an_exit_without_the_login_fails_even_on_code_zero(self) -> None:
        rt = self.runtime(paste=True)
        stream, session, _ = await self.start(rt)
        await signin.submit(rt, session, "zero", start_path="/s")
        self.assertEqual((await _next(stream))["message"], "Fake login failed. Please try again.")

    async def test_an_expired_link_is_issued_again(self) -> None:
        os.environ["EXPIRE"] = "1"
        stream, _, url = await self.start(self.runtime(paste=True))
        self.assertTrue(url.endswith("run=1"))
        self.assertEqual((await _next(stream))["line"], signin.EXPIRED_LINE)
        self.assertTrue((await _next(stream))["url"].endswith("run=2"))

    async def test_callback_refusals(self) -> None:
        rt = self.runtime(paste=True)
        with self.assertRaises(LifecycleError) as caught:
            await signin.submit(rt, "s", "good", start_path="/start")
        self.assertEqual((caught.exception.code, caught.exception.status), ("no_session", 409))
        self.assertIn("POST /start", caught.exception.message)
        await self.start(rt)
        with self.assertRaises(LifecycleError) as caught:
            await signin.submit(rt, "other", "good", start_path="/start")
        self.assertEqual((caught.exception.code, caught.exception.status), ("session_mismatch", 409))

    async def test_cancel_ends_the_stream_at_once(self) -> None:
        rt = self.runtime(paste=True)
        stream, session, _ = await self.start(rt)
        self.assertEqual(signin.cancel(rt), {"ok": True, "message": "Session cancelled", "session_id": session})
        self.assertEqual((await _next(stream))["message"], signin.CANCELLED)
        self.assertEqual(signin.cancel(rt)["message"], "No active session")

    async def test_timeout(self) -> None:
        with mock.patch.object(signin, "TIMEOUT_S", 0.2), mock.patch.object(signin, "HEARTBEAT_S", 0.1):
            stream, _, _ = await self.start(self.runtime(paste=True))
            self.assertEqual((await _next(stream))["message"], "Fake login timed out")
        self.assertNotIn("fake", signin._live)


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

    async def test_two_updates_at_once_run_one(self) -> None:
        rt = self.runtime({"update": {"command": ["{binary}", "update"]}})
        started = await asyncio.gather(*(jobs.start(rt, "update", by="founder") for _ in range(2)),
                                       return_exceptions=True)
        refused = [r for r in started if isinstance(r, LifecycleError)]
        self.assertEqual([r.code for r in refused], ["job_running"])
        job = next(r for r in started if isinstance(r, jobs.Job))
        await self.finish(job)
        self.assertEqual(job.status, "done")

    async def test_a_timeout_names_the_action(self) -> None:
        os.environ["SLOW"] = "5"
        rt = self.runtime({"update": {"command": ["{binary}", "update"]}})
        with mock.patch.object(jobs, "TIMEOUT_S", 0.3):
            events = await self.finish(await jobs.start(rt, "update", by="founder"))
        self.assertEqual(events[-1]["message"], "fake update didn't finish within 0.3 s")

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

    async def test_an_approved_uninstall_never_stops_a_session_it_didnt_list(self) -> None:
        rt = self.runtime({"uninstall": {"command": ["true"]}})
        engine = mock.Mock(running=mock.Mock(return_value=["s1", "s2"]), halt=mock.AsyncMock())
        with mock.patch("services.cowork_agent.storm.engine.current", return_value=engine):
            with self.assertRaises(LifecycleError) as caught:
                await jobs.start(rt, "uninstall", force=True, may_stop={"s1"}, by="founder")
            self.assertEqual((caught.exception.code, caught.exception.extra), ("runtime_in_use", {"sessions": ["s2"]}))
            engine.halt.assert_not_called()
            engine.running.return_value = ["s1"]
            await self.finish(await jobs.start(rt, "uninstall", force=True, may_stop={"s1"}, by="founder"))
        engine.halt.assert_awaited_once_with("s1", reason="runtime_uninstalled", by="founder")

    async def test_boot_installs_a_missing_runtime_and_waits_for_it(self) -> None:
        absent = self.dir / "absent"
        rt = self.runtime({"install": {"command": ["sh", "-c", f"touch {absent}; chmod +x {absent}"]}},
                          binary=str(absent))
        with mock.patch.object(jobs.registry, "get", return_value=rt):
            job = await jobs.boot_install("fake", by="xo")
            self.assertEqual((job.status, absent.exists()), ("done", True))
            self.assertIsNone(await jobs.boot_install("fake", by="xo"))        # installed now
        self.assertIsNone(await jobs.boot_install(None, by="xo"))

    async def test_openclaw_and_hermes_spaces_install_codex_and_others_install_nothing(self) -> None:
        for agent, runtimes in (("openclaw", ["codex"]), ("hermes", ["codex"]), ("claude_code", []),
                                ("codex", []), ("cursor", [])):
            with self.subTest(agent=agent), \
                    mock.patch.object(jobs, "boot_install", mock.AsyncMock(return_value=None)) as install:
                self.assertEqual(await jobs.boot_install_required(agent, by="xo"), [])
                self.assertEqual([c.args[0] for c in install.await_args_list], runtimes)

    async def test_boot_installs_the_space_agents_required_runtimes_in_the_background_unless_skipped(self) -> None:
        import server  # noqa: PLC0415 - lazily: server's import runs its dotenv bootstrap
        from services.cowork_agent.registry import agent_registry
        for skip, calls in (("0", [mock.call("openclaw", by=mock.ANY)]), ("1", [])):
            with self.subTest(skip=skip), mock.patch.dict(os.environ, {"QUIRQ_SKIP_BOOT_INSTALL": skip}), \
                    mock.patch.object(agent_registry, "named_agent", return_value="openclaw"), \
                    mock.patch.object(agent_registry, "space_agent", return_value="openclaw"), \
                    mock.patch.object(jobs, "boot_install", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(jobs, "boot_install_required", mock.AsyncMock(return_value=[])) as required:
                await server._install_template_runtime()
                await asyncio.gather(*server._required_installs)
                self.assertEqual(required.await_args_list, calls)



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
