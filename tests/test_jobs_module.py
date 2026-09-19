"""The jobs module: module-command jobs, the tick task and the CLI commands.

A saved job is one of two kinds. ``tests/test_scheduler.py`` covers the
command kind (a subprocess) and the tick's policy; this file covers what
the module added when the scheduler moved into ``modules/jobs/``:

* the **module-command kind** ``{"module", "command", "args", "timeout"}``
  runs another module's command in this process through
  ``services.modules.commands()``, on the server's loop when the tick task
  attached one, with the module's ``commands`` switch respected and the
  same run line and log entry as a command job;
* the **tick task** ``TASKS = [Task("tick", ...)]``: one ``service.tick()``
  per watcher tick interval, off the event loop, supervised. The watcher
  no longer drives the scheduler (``tests/test_watcher_scheduler.py`` did
  that; its scenarios live here now);
* the **CLI commands** ``python -m quirq jobs list|run|runs`` and the
  ``/api/schedules`` alias the routes keep.

Hermetic: an empty state root per test, ``now`` injected, and the module
commands a job calls are stubbed through the registry's ``commands()``
(patched on ``services.modules``, which the scheduler reads at call time)
so no real module runs. The switch itself is real: ``registry.override``
writes ``settings/modules.json`` under the sandbox root.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional
from unittest.mock import patch

from modules.jobs import commands, scheduler, service, store, tasks
from services import modules as registry
from services.errors import ServiceError
from services.periodic import run_forever
from services.supervisor import Task
from tests.support import SandboxTestCase

PY = sys.executable
T0 = datetime(2026, 9, 11, 10, 0, 0, tzinfo=timezone.utc)


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _module_job(command: str, *, args: Optional[list[str]] = None, timeout: float = 5,
                every: Optional[int] = None, **fields: Any) -> dict:
    return {"name": f"call {command}", "module": "jobs", "command": command,
            "args": list(args or []), "timeout": timeout, "every_seconds": every, **fields}


def _command_job(name: str = "shell", code: str = "print('result')", every: Optional[int] = None) -> dict:
    return {"name": name, "command": {"argv": [PY, "-c", code], "timeout": 30}, "every_seconds": every}


class _JobsSandbox(SandboxTestCase):
    """An empty state root, a clean registry and no runs in memory."""

    copy_fixtures = False

    def setUp(self) -> None:
        super().setUp()
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)
        scheduler.reset_state()
        self.addCleanup(scheduler.reset_state)
        self.addCleanup(self._join_runs)

    def _join_runs(self) -> None:
        for run in list(scheduler._running.values()):
            if run.thread is not None:
                run.thread.join(10)

    @contextmanager
    def commands(self, table: dict[str, Callable[..., Any]]) -> Iterator[None]:
        """The jobs module's own commands replaced by ``table`` for the span:
        the job's ``module`` is real (its title and switch are the
        registry's), the command it calls is the test's."""
        with patch.object(registry, "commands", return_value={"jobs": dict(table)}):
            yield

    def _run(self, job_id: str, *, now: datetime = T0) -> dict:
        """Run now, wait for the thread, harvest: the job's ``last_result``."""
        scheduler.run_now(job_id, now=now)
        scheduler._running[job_id].thread.join(10)
        report = scheduler.tick(now=now + timedelta(seconds=1))
        self.assertEqual(report.finished, [job_id])
        return scheduler.get_job(job_id)["last_result"]

    @staticmethod
    def _history(job_id: str) -> list[dict]:
        return [json.loads(line) for line in store.runs_file(job_id).read_text(encoding="utf-8").splitlines()]


# ── The module-command kind ──────────────────────────────────────────────────


class ModuleJobDefinitionTests(_JobsSandbox):
    def test_a_module_job_names_a_declared_command(self) -> None:
        with self.commands({"hello": lambda args: {"hi": args}}):
            job = scheduler.create_job(_module_job("hello", args=["a"], timeout=5), now=T0)
        self.assertEqual((job["module"], job["command"], job["args"], job["timeout"]), ("jobs", "hello", ["a"], 5.0))
        self.assertIsNone(job["every_seconds"])
        self.assertIsNone(job["next_run"], "no interval and no first_run_at: manual only")
        stored = json.loads(store.jobs_file().read_text(encoding="utf-8"))["jobs"][job["id"]]
        self.assertEqual((stored["module"], stored["command"], stored["args"]), ("jobs", "hello", ["a"]))
        self.assertEqual(stored["timeout"], 5.0)

    def test_bad_module_jobs_are_refused_and_write_nothing(self) -> None:
        without_timeout = {k: v for k, v in _module_job("hello").items() if k != "timeout"}
        bad = [
            ({**_module_job("hello"), "module": "no-such"}, "unknown module"),
            (_module_job("nope"), "has no command"),
            (without_timeout, "timeout is required"),
            (_module_job("hello", timeout=0), "positive number"),
            ({**_module_job("hello"), "args": "a"}, "list of strings"),
            ({**_module_job("hello"), "args": [1]}, "list of strings"),
            ({**_module_job("hello"), "command": {"argv": ["x"], "timeout": 1}}, "name of one of the module's commands"),
            ({**_command_job(), "args": []}, "belong to a module job"),
            ({**_command_job(), "timeout": 5}, "belong to a module job"),
        ]
        with self.commands({"hello": lambda args: None}):
            for payload, needle in bad:
                with self.subTest(payload=payload), self.assertRaises(scheduler.InvalidJobError) as ctx:
                    scheduler.create_job(payload, now=T0)
                self.assertIn(needle, str(ctx.exception))
        self.assertFalse(store.jobs_file().exists())
        self.assertFalse(store.state_file().exists())

    def test_an_update_that_changes_kind_carries_nothing_of_the_other(self) -> None:
        with self.commands({"hello": lambda args: None}):
            job = scheduler.create_job(_command_job("j"), now=T0)
            self.assertIn("argv", job["command"])
            as_module = scheduler.update_job(job["id"], _module_job("hello", timeout=5), now=T0)
            self.assertEqual((as_module["module"], as_module["command"]), ("jobs", "hello"))
            back = scheduler.update_job(job["id"], _command_job("j"), now=T0)
        self.assertIn("argv", back["command"])
        for key in ("module", "args", "timeout"):
            self.assertNotIn(key, back)


class ModuleJobRunTests(_JobsSandbox):
    def test_runs_in_process_and_records_the_same_run_line_as_a_command_job(self) -> None:
        calls: list[list[str]] = []

        def hello(args: list[str]) -> dict:
            calls.append(list(args))
            return {"greeted": args}

        with self.commands({"hello": hello}):
            module_job = scheduler.create_job(_module_job("hello", args=["a", "b"]), now=T0)
            shell_job = scheduler.create_job(_command_job(), now=T0)
            result = self._run(module_job["id"])
            shell_result = self._run(shell_job["id"])
        self.assertEqual(calls, [["a", "b"]])
        self.assertEqual((result["status"], result["returncode"], result["trigger"]), ("ok", 0, "manual"))
        self.assertEqual(json.loads(result["output_tail"]), {"greeted": ["a", "b"]})
        self.assertEqual(set(result), set(shell_result), "one record shape for both kinds")
        [line] = self._history(module_job["id"])
        self.assertEqual(list(line)[:3], ["ts", "type", "job_id"])
        self.assertEqual((line["type"], line["job_id"], line["status"]), (store.RUN_EVENT_TYPE, module_job["id"], "ok"))
        self.assertEqual(set(line), set(self._history(shell_job["id"])[0]), "one history line for both kinds")
        log_path = store.log_file(module_job["id"])
        self.assertEqual(log_path.parent, self.sandbox.state / "logs" / "jobs")
        self.assertIn("$ quirq jobs hello a b", log_path.read_text(encoding="utf-8"))

    def test_the_commands_switch_is_respected_at_run_time(self) -> None:
        with self.commands({"hello": lambda args: {"ok": True}}):
            job = scheduler.create_job(_module_job("hello"), now=T0)
            registry.override("jobs", {"commands": False})
            self.assertFalse(registry.enabled("jobs", "commands"))
            off = self._run(job["id"])
            self.assertEqual((off["status"], off["returncode"]), ("failed", 1))
            self.assertIn("Jobs commands are off in Setup", off["output_tail"])
            registry.override("jobs", {"commands": True})
            self.assertEqual(self._run(job["id"], now=_at(10))["status"], "ok")
        self.assertEqual([r["status"] for r in scheduler.list_runs(job["id"])], ["ok", "failed"])

    def test_a_command_that_raises_is_a_failed_run_with_its_message(self) -> None:
        def boom(args: list[str]) -> None:
            raise ServiceError("nope", "bad thing happened")

        def crash(args: list[str]) -> None:
            raise RuntimeError("kaboom")

        with self.commands({"boom": boom, "crash": crash}):
            a = scheduler.create_job(_module_job("boom"), now=T0)
            b = scheduler.create_job(_module_job("crash"), now=T0)
            ra, rb = self._run(a["id"]), self._run(b["id"])
        self.assertEqual((ra["status"], ra["returncode"], ra["output_tail"]), ("failed", 1, "nope: bad thing happened"))
        self.assertEqual((rb["status"], rb["output_tail"]), ("failed", "RuntimeError: kaboom"))

    def test_an_awaitable_result_is_awaited_and_bounded_by_the_timeout(self) -> None:
        seen: dict[str, Any] = {}

        async def which(args: list[str]) -> dict:
            seen["loop"] = asyncio.get_running_loop()
            return {"args": args}

        async def hang(args: list[str]) -> None:
            await asyncio.sleep(30)

        with self.commands({"which": which, "hang": hang}):
            ok = scheduler.create_job(_module_job("which", args=["x"]), now=T0)
            slow = scheduler.create_job(_module_job("hang", timeout=0.2), now=T0)
            r_ok, r_slow = self._run(ok["id"]), self._run(slow["id"])
        self.assertEqual(json.loads(r_ok["output_tail"]), {"args": ["x"]})
        self.assertIsNotNone(seen["loop"], "no loop attached: the run awaited it on a loop of its own")
        self.assertEqual(r_slow["status"], "failed")
        self.assertIn("timed out after 0.2s", r_slow["output_tail"])

    def test_with_the_tick_task_attached_the_coroutine_runs_on_the_servers_loop(self) -> None:
        seen: dict[str, Any] = {}

        async def which(args: list[str]) -> dict:
            seen["loop"] = asyncio.get_running_loop()
            return {"on": "server loop"}

        with self.commands({"which": which}):
            job = scheduler.create_job(_module_job("which"), now=T0)

            async def scenario() -> tuple[asyncio.AbstractEventLoop, scheduler.TickReport]:
                loop = asyncio.get_running_loop()
                service.attach_loop(loop)
                try:
                    await asyncio.to_thread(scheduler.run_now, job["id"], now=T0)
                    await asyncio.to_thread(scheduler._running[job["id"]].thread.join, 10)
                    return loop, await tasks.tick_once()
                finally:
                    service.detach_loop()

            loop, report = asyncio.run(scenario())
        self.assertIs(seen["loop"], loop)
        self.assertEqual(report.finished, [job["id"]])
        self.assertEqual(json.loads(scheduler.get_job(job["id"])["last_result"]["output_tail"]), {"on": "server loop"})
        self.assertIsNone(scheduler._loop)

    def test_a_scheduled_module_job_is_launched_by_the_tick(self) -> None:
        with self.commands({"hello": lambda args: {"n": 1}}):
            job = scheduler.create_job(_module_job("hello", every=60), now=T0)
            self.assertTrue(scheduler.tick(now=_at(59)).quiet)
            self.assertEqual(scheduler.tick(now=_at(60)).started, [job["id"]])
            scheduler._running[job["id"]].thread.join(10)
            self.assertEqual(scheduler.tick(now=_at(61)).finished, [job["id"]])
        result = scheduler.get_job(job["id"])["last_result"]
        self.assertEqual((result["trigger"], result["status"]), ("schedule", "ok"))
        self.assertEqual(json.loads(result["output_tail"]), {"n": 1})


# ── The tick task ────────────────────────────────────────────────────────────


class TickTaskTests(_JobsSandbox):
    def test_the_module_declares_one_supervised_tick(self) -> None:
        [task] = tasks.TASKS
        self.assertIsInstance(task, Task)
        self.assertEqual(task.name, "tick")
        self.assertIs(task.start, tasks.start_tick)
        self.assertIs(task.enabled, service.scheduler_enabled)
        self.assertTrue(task.description)
        self.assertEqual([s.task.name for s in registry.tasks() if s.module == "jobs"], ["tick"])
        self.assertTrue(registry.enabled("jobs", "tasks", "tick"))

    def test_tick_once_runs_one_tick_off_the_loop_and_logs_only_when_something_happened(self) -> None:
        job = scheduler.create_job(_command_job("j", every=60), now=T0)
        with patch.object(service, "tick", side_effect=lambda now=None: scheduler.tick(now=_at(59))), \
                patch.object(tasks.logger, "info") as info:
            report = asyncio.run(tasks.tick_once())
        self.assertTrue(report.quiet)
        info.assert_not_called()
        with patch.object(service, "tick", side_effect=lambda now=None: scheduler.tick(now=_at(60))), \
                self.assertLogs(tasks.logger, logging.INFO) as logs:
            report = asyncio.run(tasks.tick_once())
        self.assertEqual(report.started, [job["id"]])
        self.assertIn(job["id"], logs.output[0])
        scheduler._running[job["id"]].thread.join(10)

    def test_a_long_run_spans_many_ticks_and_is_harvested_when_done(self) -> None:
        # The "10 minute job on a 1 second tick" case, once the watcher's:
        # one start, quiet ticks while it runs, one finish.
        job = scheduler.create_job(_command_job("long", every=3600), now=T0)
        flag = self.sandbox.base / "go"
        block = "import os, sys, time\nwhile not os.path.exists(sys.argv[1]):\n    time.sleep(0.02)\n"
        scheduler.update_job(job["id"], {"name": "long", "every_seconds": 3600,
                                         "command": {"argv": [PY, "-c", block, str(flag)], "timeout": 30}}, now=T0)
        self.assertEqual(scheduler.tick(now=_at(3600)).started, [job["id"]])
        for s in range(3601, 3611):
            report = scheduler.tick(now=_at(s))
            self.assertEqual((report.started, report.finished), ([], []), f"tick at +{s}s")
        self.assertEqual(len(scheduler._running), 1)
        flag.write_text("go", encoding="utf-8")
        scheduler._running[job["id"]].thread.join(10)
        self.assertEqual(scheduler.tick(now=_at(3612)).finished, [job["id"]])
        result = scheduler.get_job(job["id"])["last_result"]
        self.assertEqual((result["status"], result["trigger"]), ("ok", "schedule"))

    def test_disabled_scheduler_ticks_quietly_and_starts_nothing(self) -> None:
        scheduler.create_job(_command_job("j", every=60), now=T0)
        with patch.dict("os.environ", {"XO_SCHEDULER_ENABLED": "false"}):
            self.assertFalse(service.scheduler_enabled())
            report = scheduler.tick(now=_at(120))
        self.assertFalse(report.enabled)
        self.assertEqual(report.started, [])
        self.assertEqual(scheduler._running, {})

    def test_start_tick_attaches_the_running_loop_for_the_span_of_the_loop(self) -> None:
        seen: dict[str, Any] = {}

        async def fake_run_forever(name: str, tick: Any, *, interval_s: Any, **kwargs: Any) -> None:
            seen.update(name=name, tick=tick, loop=scheduler._loop, interval=interval_s())

        with patch.object(tasks, "run_forever", fake_run_forever):
            async def scenario() -> asyncio.AbstractEventLoop:
                await tasks.start_tick()
                return asyncio.get_running_loop()

            loop = asyncio.run(scenario())
        self.assertIs(seen["loop"], loop)
        self.assertIs(seen["tick"], tasks.tick_once)
        self.assertGreater(seen["interval"], 0)
        self.assertIsNone(scheduler._loop, "detached when the loop ends")

    def test_a_tick_that_raises_is_logged_and_the_loop_goes_on(self) -> None:
        # The supervised loop is services.periodic.run_forever: a failing
        # tick is a warning, not the end of scheduling.
        async def scenario() -> list[str]:
            done = asyncio.Event()
            loop = asyncio.get_running_loop()
            outcomes: list[Any] = [RuntimeError("boom")]

            def flaky() -> scheduler.TickReport:
                if outcomes:
                    raise outcomes.pop()
                loop.call_soon_threadsafe(done.set)
                return scheduler.TickReport()

            with patch.object(service, "tick", side_effect=flaky), \
                    self.assertLogs(tasks.logger, logging.WARNING) as logs:
                runner = asyncio.create_task(
                    run_forever("jobs tick", tasks.tick_once, interval_s=lambda: 0.01, logger=tasks.logger))
                await asyncio.wait_for(done.wait(), 10)
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner
            return logs.output

        output = asyncio.run(scenario())
        self.assertTrue(any("tick failed" in line for line in output), output)


# ── Commands, files and routes ───────────────────────────────────────────────


class CommandsTests(_JobsSandbox):
    def test_the_cli_commands_are_the_facade(self) -> None:
        self.assertEqual(set(commands.COMMANDS), {"list", "run", "runs"})
        self.assertEqual(registry.commands()["jobs"], commands.COMMANDS)
        self.assertEqual(commands.list_([]), {"jobs": []})
        job = scheduler.create_job(_command_job("j"), now=T0)
        for bad in (commands.run, commands.runs):
            with self.assertRaises(ServiceError):
                bad([])
        started = commands.run([job["id"]])
        self.assertEqual((started["ok"], started["started"], started["job"]["id"]), (True, True, job["id"]))
        scheduler._running[job["id"]].thread.join(10)
        listed = commands.runs([job["id"], "5"])
        self.assertEqual(listed["job_id"], job["id"])
        self.assertEqual([r["status"] for r in listed["runs"]], ["ok"])
        self.assertEqual(listed["log_path"], str(store.log_file(job["id"])))
        self.assertEqual([j["id"] for j in commands.list_([])["jobs"]], [job["id"]])


class FilesAndRoutesTests(unittest.TestCase):
    def setUp(self) -> None:
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)

    def test_the_file_table_names_the_three_files_the_module_writes(self) -> None:
        self.assertEqual([f.pattern for f in store.FILES],
                         ["jobs/jobs.json", "jobs/state.json", "jobs/runs/<id>.jsonl"])
        self.assertEqual([f.role for f in store.FILES], ["decision", "fact", "record"])
        self.assertTrue(store.FILES[2].matches("jobs/runs/nightly-tests-a1b2c3.jsonl"))
        self.assertEqual([f.pattern for m, f in registry.files() if m == "jobs"], [f.pattern for f in store.FILES])
        self.assertEqual(registry.get("jobs").folder, "jobs")

    def test_the_routes_keep_the_seven_schedules_paths_under_the_manifest_alias(self) -> None:
        from modules.jobs.routes import router

        self.assertEqual(registry.get("jobs").aliases, ("/api/schedules",))
        served = sorted((route.path, method) for route in router.routes for method in route.methods)
        self.assertEqual(served, [
            ("/api/schedules", "GET"), ("/api/schedules", "POST"),
            ("/api/schedules/{job_id}", "DELETE"), ("/api/schedules/{job_id}", "GET"),
            ("/api/schedules/{job_id}", "PUT"),
            ("/api/schedules/{job_id}/run", "POST"), ("/api/schedules/{job_id}/runs", "GET"),
        ])
        self.assertEqual(registry.event_types().get(store.RUN_EVENT_TYPE), "jobs")


if __name__ == "__main__":
    unittest.main()
