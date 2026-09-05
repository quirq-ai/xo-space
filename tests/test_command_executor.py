from __future__ import annotations

import asyncio
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class SkillCatalogArgvTests(unittest.TestCase):
    def test_entries_resolve_to_argv_lists(self) -> None:
        from services.cowork_agent import skill_catalog as sc

        entry = sc._normalize({"name": "x", "commands": ["npm install -g pkg", ["npx", "skills", "add", "a/b"]]})
        self.assertEqual(entry["commands"], [["npm", "install", "-g", "pkg"], ["npx", "skills", "add", "a/b"]])
        self.assertIsNone(sc._normalize({"name": "x", "command": "npm i a && rm -rf /"}))
        self.assertIsNone(sc._normalize({"name": "x", "commands": [["npm", 3]]}))
        self.assertIsNone(sc._normalize({"name": "x", "commands": []}))

    def test_install_runs_each_step_as_argv_with_placeholders_per_token(self) -> None:
        from services.cowork_agent import skill_catalog as sc

        entry = sc._normalize({"name": "demo", "commands": ["tool --dir {skills_dir}", ["echo", "done"]], "timeout_seconds": 7})
        seen = []

        async def fake_run(argv, *, cwd=None, timeout=None, env=None, log_path=None, log_label=""):
            seen.append((list(argv), cwd, timeout))
            return CommandResult(argv=list(argv), returncode=0, output="ok\n", duration_seconds=0.01)

        with patch.object(sc, "load_catalog", return_value={"demo": entry}), \
             patch.object(sc, "_expand_placeholders", side_effect=lambda t: t.replace("{skills_dir}", "/home/x y/skills")), \
             patch.object(sc, "run", new=fake_run):
            result = run(sc.install("demo"))
        self.assertTrue(result["ok"])
        self.assertEqual(seen, [(["tool", "--dir", "/home/x y/skills"], None, 7), (["echo", "done"], None, 7)])

    def test_install_stops_at_first_failed_step(self) -> None:
        from services.cowork_agent import skill_catalog as sc

        entry = sc._normalize({"name": "demo", "commands": [["a"], ["b"]]})
        calls = []

        async def fake_run(argv, **kw):
            calls.append(argv[0])
            return CommandResult(argv=list(argv), returncode=1, output="nope", duration_seconds=0.0)

        with patch.object(sc, "load_catalog", return_value={"demo": entry}), patch.object(sc, "run", new=fake_run):
            result = run(sc.install("demo"))
        self.assertFalse(result["ok"])
        self.assertEqual(calls, ["a"])
        self.assertEqual(result["steps"][0]["stderr"], "nope")


class OneExecutorTests(unittest.TestCase):
    """Architecture guard. Two rules:

    1. No shell, anywhere: no `shell=True`, `create_subprocess_shell`,
       `os.system` or `os.popen` outside the runner's own docstring.
    2. Direct `subprocess` / `create_subprocess_exec` calls are allowed only in
       `utils/commands.py` and in the files listed in MIGRATION_BACKLOG. That
       list may only shrink: converting a file to `utils.commands` means
       removing it here. Adding a new direct call anywhere fails this test.
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
    SHELL = re.compile(r"shell\s*=\s*True|create_subprocess_shell\(|os\.system\(|os\.popen\(")

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

    def test_backlog_entries_still_need_migrating(self) -> None:
        # A file that no longer calls subprocess directly must leave the list,
        # so the backlog is an honest count rather than a permanent exemption.
        present = {rel: txt for rel, txt in self._files()}
        stale = sorted(rel for rel in self.MIGRATION_BACKLOG if rel in present and not self.DIRECT.search(present[rel]))
        self.assertEqual(stale, [], "these files are migrated; remove them from MIGRATION_BACKLOG")
