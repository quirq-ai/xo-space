from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import utils.commands as commands
from utils.commands import (
    CommandResult,
    CommandSpec,
    CommandSpecError,
    run_spec,
    safe_arg,
    split_command,
)

ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class CommandSpecTests(unittest.TestCase):
    def test_argv_spec_round_trips(self) -> None:
        spec = CommandSpec.from_json({"argv": ["git", "fetch", "origin"], "cwd": "/tmp", "timeout": 5, "env": {"A": "1"}})
        self.assertEqual(spec.argv, ["git", "fetch", "origin"])
        self.assertEqual((spec.cwd, spec.timeout, spec.env), ("/tmp", 5.0, {"A": "1"}))

    def test_rejects_malformed_specs(self) -> None:
        bad = [
            {},                                           # nothing to run
            {"argv": []},                                 # empty
            {"argv": ["git", 3]},                         # non-string
            {"argv": ["git"], "command": "git"},          # both forms
            {"argv": ["git"], "shell": True},             # unknown key (there is no shell knob)
            {"argv": ["git"], "timeout": 0},
            {"argv": ["git"], "timeout": True},
            {"argv": ["git"], "cwd": ""},
            {"argv": ["git"], "env": {"A": 1}},
            "git fetch",                                  # not an object
        ]
        for obj in bad:
            with self.subTest(obj=obj), self.assertRaises(CommandSpecError):
                CommandSpec.from_json(obj)

    def test_string_command_is_split_without_a_shell(self) -> None:
        spec = CommandSpec.from_json({"command": 'npx skills add okx/onchainos-skills --yes -g'})
        self.assertEqual(spec.argv, ["npx", "skills", "add", "okx/onchainos-skills", "--yes", "-g"])
        spec = CommandSpec.from_json({"command": 'tool --dir "/home/some user/skills"'})
        self.assertEqual(spec.argv, ["tool", "--dir", "/home/some user/skills"])

    def test_shell_operators_are_refused(self) -> None:
        for cmd in ("a && b", "a || b", "a | b", "a; b", "a > f", "a < f", "echo `id`", "echo $(id)"):
            with self.subTest(cmd=cmd), self.assertRaises(CommandSpecError):
                split_command(cmd)
        with self.assertRaises(CommandSpecError):
            split_command('unbalanced "quote')


class SafeArgTests(unittest.TestCase):
    def test_refuses_options_and_junk(self) -> None:
        self.assertEqual(safe_arg("main"), "main")
        self.assertEqual(safe_arg("-v", allow_option=True), "-v")
        for value in ("", "--upload-pack=evil", "-c", None, 3):
            with self.subTest(value=value), self.assertRaises(CommandSpecError):
                safe_arg(value)


class RunSpecTests(unittest.TestCase):
    """The runner is the one place that may spawn a process, so these are the
    only tests in the suite that do; they spawn this interpreter."""

    def test_runs_and_reports_exit_code_and_output(self) -> None:
        spec = CommandSpec.from_json({"argv": [sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"], "timeout": 30})
        res = run(run_spec(spec))
        self.assertIsInstance(res, CommandResult)
        self.assertEqual(res.returncode, 3)
        self.assertFalse(res.ok)
        self.assertIn("hi", res.output)

    def test_timeout_kills_and_is_reported(self) -> None:
        spec = CommandSpec.from_json({"argv": [sys.executable, "-c", "import time; time.sleep(10)"], "timeout": 0.5})
        res = run(run_spec(spec))
        self.assertTrue(res.timed_out)
        self.assertFalse(res.ok)

    def test_missing_binary_is_a_result_not_an_exception(self) -> None:
        res = run(run_spec(CommandSpec.from_json({"argv": ["definitely-not-a-binary-xyz"]})))
        self.assertTrue(res.binary_missing)

    def test_separate_stderr_keeps_stdout_clean(self) -> None:
        from utils.commands import run as run_cmd, run_sync
        code = "import sys; sys.stdout.write('out'); sys.stderr.write('err')"
        res = run(run_cmd([sys.executable, "-c", code], separate_stderr=True, timeout=30))
        self.assertEqual((res.output, res.stderr, res.stdout), ("out", "err", "out"))
        merged = run(run_cmd([sys.executable, "-c", code], timeout=30))
        self.assertIn("out", merged.output)
        self.assertIn("err", merged.output)
        self.assertEqual(merged.stderr, "")
        sync = run_sync([sys.executable, "-c", code], separate_stderr=True, timeout=30)
        self.assertEqual((sync.output, sync.stderr), ("out", "err"))

    def test_stdin_input_reaches_the_child(self) -> None:
        from utils.commands import run as run_cmd, run_sync
        code = "import sys; sys.stdout.write(sys.stdin.read().upper())"
        res = run(run_cmd([sys.executable, "-c", code], input=b"secret", timeout=30))
        self.assertEqual(res.output, "SECRET")
        sync = run_sync([sys.executable, "-c", code], input=b"abc", timeout=30)
        self.assertEqual(sync.output, "ABC")

    def test_without_input_stdin_is_closed_so_prompts_cannot_hang(self) -> None:
        from utils.commands import run as run_cmd
        code = "import sys; sys.stdout.write(repr(sys.stdin.read()))"
        res = run(run_cmd([sys.executable, "-c", code], timeout=30))
        self.assertEqual(res.output, "''")

    def test_inherit_output_captures_nothing_but_reports_exit(self) -> None:
        from utils.commands import run_sync
        res = run_sync([sys.executable, "-c", "import sys; sys.exit(0)"], inherit_output=True, timeout=30)
        self.assertTrue(res.ok)
        self.assertEqual(res.output, "")

    def test_spawn_detached_reports_spawn_failure_only(self) -> None:
        from utils.commands import spawn_detached
        ok = spawn_detached([sys.executable, "-c", "pass"])
        self.assertTrue(ok.ok)
        missing = spawn_detached(["definitely-not-a-binary-xyz"])
        self.assertTrue(missing.binary_missing)

    def test_default_command_log_records_shape_and_caps_output(self) -> None:
        from utils.commands import run_sync

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / ".quirq"
            payload = "A" * 5000
            with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(state_root)}, clear=False):
                result = run_sync([sys.executable, "-c", f"print({payload!r})"], cwd=str(ROOT), timeout=30)
            self.assertTrue(result.ok)
            text = (state_root / "commands.log").read_text(encoding="utf-8")
        self.assertIn("$", text)
        self.assertIn(f"cwd: {ROOT}", text)
        self.assertRegex(text, r"\[0; \d+\.\d{3}s\]")
        self.assertIn("...[truncated ", text)
        self.assertIn("A" * 100, text)

    def test_default_command_log_redacts_known_secret_shapes(self) -> None:
        result = CommandResult(
            argv=["git"],
            returncode=0,
            output=(
                "token ghp_secretvalue\n"
                "sk-live-secret\n"
                "AUTHORIZATION: basic abc123\n"
                "Authorization: token github_pat_secretvalue\n"
                "--token=gho_secretvalue"
            ),
            duration_seconds=0.1,
        )
        entry = commands._render_log_entry(
            "2026-01-01T00:00:00+00:00",
            "",
            [
                "git",
                "-c",
                "http.https://github.com/.extraheader=AUTHORIZATION: basic abc123",
                "--token",
                "ghp_secretvalue",
                "--code=ak_secretvalue",
            ],
            result,
            cwd="/tmp/work",
        )
        self.assertIn("http.https://github.com/.extraheader=AUTHORIZATION: basic [REDACTED]", entry)
        self.assertIn("--token [REDACTED]", entry)
        self.assertIn("--code=[REDACTED]", entry)
        self.assertIn("Authorization: token [REDACTED]", entry)
        for secret in (
            "abc123",
            "ghp_secretvalue",
            "gho_secretvalue",
            "github_pat_secretvalue",
            "sk-live-secret",
            "ak_secretvalue",
        ):
            self.assertNotIn(secret, entry)

    def test_rotation_keeps_one_generation(self) -> None:
        from utils.commands import run_sync

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / ".quirq"
            with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(state_root)}, clear=False), \
                 patch.object(commands, "_COMMAND_LOG_MAX_BYTES", 200):
                first = run_sync([sys.executable, "-c", "print('first entry payload')"], timeout=30)
                second = run_sync([sys.executable, "-c", "print('second entry payload')"], timeout=30)
            self.assertTrue(first.ok)
            self.assertTrue(second.ok)
            current = (state_root / "commands.log").read_text(encoding="utf-8")
            rotated = (state_root / "commands.log.1").read_text(encoding="utf-8")
        self.assertIn("second entry payload", current)
        self.assertNotIn("first entry payload", current)
        self.assertIn("first entry payload", rotated)

    def test_off_switch_disables_default_log_but_keeps_explicit_log_path(self) -> None:
        from utils.commands import run_sync

        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / ".quirq"
            extra_log = Path(tmp) / "explicit.log"
            env_override_log = Path(tmp) / "from-env.log"
            with patch.dict(
                os.environ,
                {
                    "QUIRQ_STATE_ROOT": str(state_root),
                    "QUIRQ_COMMAND_LOG": "off",
                    "QUIRQ_COMMAND_LOG_PATH": str(env_override_log),
                },
                clear=False,
            ):
                result = run_sync([sys.executable, "-c", "print('ok')"], log_path=extra_log, timeout=30)
            self.assertTrue(result.ok)
            self.assertFalse((state_root / "commands.log").exists())
            self.assertFalse(env_override_log.exists())
            self.assertIn("ok", extra_log.read_text(encoding="utf-8"))

    def test_logging_failure_warns_once_and_does_not_change_result(self) -> None:
        from utils.commands import run_sync

        with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": "/tmp/quirq-tests"}, clear=False), \
             patch.object(commands, "_write_log", side_effect=OSError("disk full")), \
             patch.object(commands.log, "warning") as warning, \
             patch.object(commands, "_COMMAND_LOG_WARNING_EMITTED", False), \
             patch.object(commands, "_FAILED_COMMAND_LOG_PATHS", set()):
            first = run_sync([sys.executable, "-c", "print('one')"], timeout=30)
            second = run_sync([sys.executable, "-c", "print('two')"], timeout=30)
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(warning.call_count, 1)

    @unittest.skipIf(os.name != "posix", "process groups are POSIX")
    def test_timeout_kills_the_whole_process_group(self) -> None:
        """`sh` here exits only when its background child does. Killing just
        `sh` leaves the child holding the stdout pipe, and the runner would
        wait for it (30 s) instead of honouring the 0.5 s timeout."""
        started = time.monotonic()
        res = run(commands.run(["sh", "-c", "sleep 30 & wait"], timeout=0.5))
        self.assertTrue(res.timed_out)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_kill_race_after_timeout_is_a_result_not_an_exception(self) -> None:
        """If the child exits in the instant between the timeout and the kill,
        asyncio raises ProcessLookupError from kill(); the runner must swallow
        it and still report the timeout."""
        class RacyProc:
            returncode = -9
            calls = 0

            async def communicate(self, input=None):
                self.calls += 1
                if self.calls == 1:
                    await asyncio.sleep(3600)        # the first read outlives the timeout
                return b"", b""

            def kill(self):
                raise ProcessLookupError()           # already gone

        async def fake_exec(*argv, **kwargs):
            return RacyProc()

        with patch.object(commands.asyncio, "create_subprocess_exec", new=fake_exec):
            res = run(commands.run(["anything"], timeout=0.01))
        self.assertTrue(res.timed_out)
        self.assertFalse(res.ok)
        self.assertEqual(res.returncode, -9)         # the kill signal, as subprocess reports it

    def test_run_spec_passes_capture_options_through(self) -> None:
        seen = {}

        async def fake_run(argv, **kw):
            seen.update(argv=list(argv), **kw)
            return CommandResult(argv=list(argv), returncode=0, output="", duration_seconds=0.0)

        spec = CommandSpec.from_json({"argv": ["x"], "cwd": "/tmp", "timeout": 3})
        with patch.object(commands, "run", new=fake_run):
            run(run_spec(spec, separate_stderr=True))
        self.assertEqual((seen["argv"], seen["cwd"], seen["timeout"], seen["separate_stderr"]),
                         (["x"], "/tmp", 3.0, True))


class SkillCatalogArgvTests(unittest.TestCase):
    def test_entries_resolve_to_argv_lists(self) -> None:
        from services.cowork_agent import skill_catalog as sc

        entry = sc._normalize({"name": "x", "commands": ["npm install -g pkg", ["npx", "skills", "add", "a/b"]]})
        self.assertEqual(entry["commands"], [["npm", "install", "-g", "pkg"], ["npx", "skills", "add", "a/b"]])
        self.assertIsNone(sc._normalize({"name": "x", "command": "npm i a && rm -rf /"}))
        self.assertIsNone(sc._normalize({"name": "x", "commands": [["npm", 3]]}))
        self.assertIsNone(sc._normalize({"name": "x", "commands": []}))

    def test_entries_are_validated_by_the_spec_not_by_the_catalog(self) -> None:
        """The one door: cwd/timeout rules come from CommandSpec.from_json, so a
        bad value is rejected by the same code that rejects it everywhere else."""
        from services.cowork_agent import skill_catalog as sc

        entry = sc._normalize({"name": "x", "command": "a", "cwd": "/tmp", "timeout_seconds": 9})
        self.assertIsInstance(entry["specs"][0], CommandSpec)
        self.assertEqual((entry["specs"][0].cwd, entry["specs"][0].timeout), ("/tmp", 9.0))
        self.assertEqual((entry["cwd"], entry["timeout_seconds"]), ("/tmp", 9.0))
        for bad in ({"cwd": 5}, {"cwd": ""}, {"timeout_seconds": 0}, {"timeout_seconds": True}, {"timeout_seconds": "3"}):
            with self.subTest(bad=bad):
                self.assertIsNone(sc._normalize({"name": "x", "command": "a", **bad}))

    def test_install_runs_each_step_as_a_spec_with_placeholders_per_token(self) -> None:
        from services.cowork_agent import skill_catalog as sc

        entry = sc._normalize({"name": "demo", "commands": ["tool --dir {skills_dir}", ["echo", "done"]],
                               "timeout_seconds": 7, "cwd": "/work"})
        seen = []

        async def fake_run_spec(spec, **kw):
            seen.append((spec.argv, spec.cwd, spec.timeout, kw.get("separate_stderr")))
            return CommandResult(argv=spec.argv, returncode=0, output="ok\n", duration_seconds=0.01)

        with patch.object(sc, "load_catalog", return_value={"demo": entry}), \
             patch.object(sc, "_expand_placeholders", side_effect=lambda t: t.replace("{skills_dir}", "/home/x y/skills")), \
             patch.object(sc, "run_spec", new=fake_run_spec):
            result = run(sc.install("demo"))
        self.assertTrue(result["ok"])
        self.assertEqual(seen, [(["tool", "--dir", "/home/x y/skills"], "/work", 7.0, True),
                                (["echo", "done"], "/work", 7.0, True)])

    def test_install_stops_at_first_failed_step_and_keeps_both_streams(self) -> None:
        from services.cowork_agent import skill_catalog as sc

        entry = sc._normalize({"name": "demo", "commands": [["a"], ["b"]]})
        calls = []

        async def fake_run_spec(spec, **kw):
            calls.append(spec.argv[0])
            return CommandResult(argv=spec.argv, returncode=1, output="partial out", stderr="nope",
                                 duration_seconds=0.0)

        with patch.object(sc, "load_catalog", return_value={"demo": entry}), patch.object(sc, "run_spec", new=fake_run_spec):
            result = run(sc.install("demo"))
        self.assertFalse(result["ok"])
        self.assertEqual(calls, ["a"])
        step = result["steps"][0]
        # the response contract predates the executor: stdout AND stderr, exit code as reported
        self.assertEqual((step["stdout"], step["stderr"], step["exit_code"]), ("partial out", "nope", 1))

    def test_a_step_that_cannot_start_or_times_out_keeps_the_old_shape(self) -> None:
        from services.cowork_agent import skill_catalog as sc

        entry = sc._normalize({"name": "demo", "command": "x"})
        outcomes = iter([
            CommandResult(argv=["x"], returncode=-1, output="x not found in PATH", duration_seconds=0.0, binary_missing=True),
            CommandResult(argv=["x"], returncode=-9, output="[timed out after 1s]", duration_seconds=1.0, timed_out=True),
        ])

        async def fake_run_spec(spec, **kw):
            return next(outcomes)

        with patch.object(sc, "load_catalog", return_value={"demo": entry}), patch.object(sc, "run_spec", new=fake_run_spec):
            missing = run(sc.install("demo"))["steps"][0]
            timed = run(sc.install("demo"))["steps"][0]
        self.assertEqual((missing["exit_code"], missing["stderr"]), (None, "failed to start command: x not found in PATH"))
        self.assertEqual((timed["timed_out"], timed["exit_code"], timed["stdout"], timed["stderr"]), (True, -9, "", ""))


class OneExecutorTests(unittest.TestCase):
    """Architecture guard (a fitness function for the one-executor rule). Three rules:

    1. No shell, anywhere: no `shell=True`, `create_subprocess_shell`,
       `os.system` / `os.popen`, the `os.exec*` / `os.spawn*` / `posix_spawn`
       family, or `pty.spawn` outside the runner's own docstring.
    2. Direct `subprocess` / `create_subprocess_exec` calls are allowed only in
       `utils/commands.py` and in the files listed in MIGRATION_BACKLOG. That
       list may only shrink: converting a file to `utils.commands` means
       removing it here. Adding a new direct call anywhere fails this test.
    3. The same for `import subprocess` / `from subprocess import ...`: a regex
       over call sites misses `from subprocess import Popen; Popen(...)`, so the
       import itself is the thing that is fenced.
    """

    SKIP_DIRS = {"venv", ".venv", "node_modules", ".git", "tests", "tests2", "docs", ".claude"}
    RUNNER = "utils/commands.py"
    MIGRATION_BACKLOG = {
        # streaming / PTY runtimes — need a live pipe, migrate last
        "config/models/claude_code/client.py",
        "config/models/codex/client.py",
        "services/cowork_agent/adapters/antigravity/adapter.py",
        "services/cowork_agent/adapters/antigravity/routes.py",
        "services/cowork_agent/adapters/claude_code/adapter.py",
        "services/cowork_agent/adapters/claude_code/remote_control.py",
        "services/cowork_agent/adapters/claude_code/session_telemetry.py",
        "services/cowork_agent/adapters/cli_status.py",
        "services/cowork_agent/adapters/codex/adapter.py",
        "services/cowork_agent/adapters/hermes/agents.py",
        "services/cowork_agent/adapters/hermes/gateway_pool.py",
        "services/cowork_agent/adapters/hermes/routes.py",
        "routers/auth/claude_setup_token.py",
        "routers/auth/codex_setup.py",
        # partly migrated: their one-shot calls use the runner; what remains is a
        # live-streamed `gh auth login --web` and rclone's stdin-streaming /
        # long-running authorize flows, which need a pipe the runner does not offer
        "services/cowork_agent/connectors/github/cli_auth.py",
        "services/cowork_agent/connectors/rclone/connector.py",
    }
    DIRECT = re.compile(r"subprocess\.(run|Popen|check_output|check_call|call)\(|create_subprocess_exec\(")
    IMPORTS = re.compile(r"^\s*(import subprocess\b|from subprocess import\b)", re.MULTILINE)
    SHELL = re.compile(
        r"shell\s*=\s*True|create_subprocess_shell\(|os\.system\(|os\.popen\("
        r"|os\.(exec[lv]p?e?|spawn[lv]p?e?|posix_spawnp?)\(|pty\.spawn\("
    )

    def _files(self):
        for p in ROOT.rglob("*.py"):
            rel = p.relative_to(ROOT)
            if any(part in self.SKIP_DIRS for part in rel.parts):
                continue
            yield rel.as_posix(), p.read_text(encoding="utf-8", errors="replace")

    def test_no_shell_anywhere(self) -> None:
        offenders = [rel for rel, txt in self._files() if rel != self.RUNNER and self.SHELL.search(txt)]
        self.assertEqual(offenders, [], "a shell path was added; build an argv and use utils.commands")

    def test_direct_subprocess_calls_only_in_the_runner_or_the_backlog(self) -> None:
        offenders = [rel for rel, txt in self._files()
                     if rel != self.RUNNER and rel not in self.MIGRATION_BACKLOG and self.DIRECT.search(txt)]
        self.assertEqual(offenders, [], "new code must call utils.commands.run / run_spec, not subprocess directly")

    def test_subprocess_is_not_imported_outside_the_runner_or_the_backlog(self) -> None:
        offenders = [rel for rel, txt in self._files()
                     if rel != self.RUNNER and rel not in self.MIGRATION_BACKLOG and self.IMPORTS.search(txt)]
        self.assertEqual(offenders, [], "import subprocess only in utils/commands.py (or a backlog file)")

    def test_backlog_entries_still_need_migrating(self) -> None:
        # A file that no longer calls subprocess directly must leave the list,
        # so the backlog is an honest count rather than a permanent exemption.
        present = {rel: txt for rel, txt in self._files()}
        stale = sorted(rel for rel in self.MIGRATION_BACKLOG if rel in present and not self.DIRECT.search(present[rel]))
        self.assertEqual(stale, [], "these files are migrated; remove them from MIGRATION_BACKLOG")
