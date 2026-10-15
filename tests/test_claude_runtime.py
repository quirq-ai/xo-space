"""The claude_code runtime against its fake CLI (storm ch 02; ch 18 §2 PR 6).

Proves what the conformance suite can't say in neutral terms: the exact
command line per mode and role, the env whitelist (no server secrets, no
decision-model keys, no inherited ``CLAUDE_CODE_*``, today's auth function's
behaviour kept), the session's own folder, the stdio host (held, answered,
failing, withdrawn), questions, runs and ``stop_task``, the rate-limit guard,
reconnect detection, ``update``, the failure paths (a crash mid-turn, an
overlong line, an interrupt that has to kill, close during a held call), and
the claude parsers for its own transcripts. No real CLI runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.adapter import ClaudeCodeAdapter
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime, command, support
from services.cowork_agent.adapters.claude_code.runtime.env import session_env
from services.cowork_agent.adapters.claude_code.runtime.session import EVENT_QUEUE_SIZE
from services.cowork_agent.adapters.claude_code._project_encoding import encode_path
from services.cowork_agent.adapters.claude_code.runtime.transcript import remove_empty_project_folder
from services.cowork_agent.adapters.claude_code.runtime.wire import ContentCounter
from tests.fakes import claude, expect, install, scenario
from tests.fakes.host import ScriptedHost
from tests.test_runtime_conformance import wait_for_event
from utils.commands import live_processes

REPO = Path(__file__).resolve().parents[1]
FIXTURE_HOME = REPO / "tests" / "fixtures" / "claude-home"
SID = "native-1"
SKIP_FLAGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
              "--dangerously-bypass-approvals-and-sandbox", "bypassPermissions")
ALLOW = c.PermissionDecision(allow=True)


def files_for(spec: c.SessionSpec, folder: Path) -> command.RunFiles:
    return command.write_run_files(folder, spec, ("Read(~/.ssh/**)", "Edit(~/.ssh/**)"))


def argv_for(spec: c.SessionSpec, *, native_id: str = "nid", resume: str | None = None) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        files = files_for(spec, Path(tmp) / "run")
        argv = command.build_argv("claude", spec, files, native_id=native_id, resume=resume)
        return [a.replace(tmp, "<tmp>") for a in argv]


class CommandLineTests(unittest.TestCase):
    BASE = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--permission-prompt-tool", "stdio"]

    def test_plain_session_exact_argv(self) -> None:
        spec = c.SessionSpec(cwd="/w", model="haiku", effort="low", permissions=c.PermissionPolicy(mode="auto"),
                             system_prompt=c.SystemPrompt(text="be brief"), add_dirs=("/extra",))
        self.assertEqual(argv_for(spec), [
            *self.BASE, "--session-id", "nid", "--model", "haiku", "--effort", "low", "--permission-mode", "auto",
            "--append-system-prompt-file", "<tmp>/run/system.txt", "--settings", "<tmp>/run/settings.json",
            "--add-dir", "/extra", "--thinking-display", "summarized"])

    def test_each_mode_maps_to_claudes_name(self) -> None:
        expected = {"ask": "default", "accept_edits": "acceptEdits", "plan": "plan", "deny_unasked": "dontAsk",
                    "auto": "auto", "bypass": "bypassPermissions"}
        for mode, name in expected.items():
            with self.subTest(mode):
                argv = argv_for(c.SessionSpec(cwd="/w", permissions=c.PermissionPolicy(mode=mode)))
                self.assertEqual(argv[argv.index("--permission-mode") + 1], name)

    def test_coordinator_is_answered_by_the_host_not_dont_ask(self) -> None:
        composio = c.McpServer(name="composio", transport="http", url="http://127.0.0.1:9/mcp")
        spec = c.SessionSpec(cwd="/w", orchestration="coordinator", permissions=c.PermissionPolicy(mode="deny_unasked"),
                             tools=c.ToolPolicy(allow=("read", "shell:git log", "shell:ls")),
                             mcp_servers=(c.McpServer(name="xo_comms", transport="http", url="http://x/mcp",
                                                      bearer_env="XO_COMMS_TOKEN", env={"XO_COMMS_TOKEN": "t"},
                                                      auto_approve=True, terminal_tools=("reply",)), composio))
        argv = argv_for(spec)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--append-system-prompt-file")]
        # No shell rule, not even the spec's own: every worker command reaches the host.
        self.assertEqual(allowed, ["Read", "mcp__xo_comms__*", "Grep", "Glob"])
        self.assertEqual(command.settings(spec, ("Read(~/.ssh/**)",))["permissions"]["ask"],
                         ["Read(~/.ssh/**)", "Bash"])
        switches = command.session_switches(spec)
        self.assertEqual(switches, {"CLAUDE_CODE_COORDINATOR_MODE": "1", "CLAUDE_CODE_MCP_PREWAIT_SERVERS": "xo_comms",
                                    "CLAUDE_CODE_TERMINAL_MCP_TOOLS": "mcp__xo_comms__reply", "XO_COMMS_TOKEN": "t"})
        self.assertEqual(command.mcp_config(spec), {"mcpServers": {
            "xo_comms": {"type": "http", "url": "http://x/mcp",
                         "headers": {"Authorization": "Bearer ${XO_COMMS_TOKEN}"}, "alwaysLoad": True,
                         "role": "comms"},
            "composio": {"type": "http", "url": "http://127.0.0.1:9/mcp"}}})
        self.assertIn(command.COORDINATOR_ADDENDUM, command.system_text(spec))
        plain = c.SessionSpec(cwd="/w", tools=c.ToolPolicy(allow=("shell:git log",)))
        self.assertIn("Bash(git log:*)", argv_for(plain))
        self.assertNotIn("Bash", command.settings(plain, ())["permissions"]["ask"])

    def test_no_skip_or_bypass_flag_unless_the_spec_asks_for_bypass(self) -> None:
        for mode in ("ask", "accept_edits", "plan", "deny_unasked", "auto"):
            for orchestration in ("none", "coordinator"):
                for isolation in ("user", "isolated"):
                    with self.subTest(mode=mode, orchestration=orchestration, isolation=isolation):
                        argv = argv_for(c.SessionSpec(cwd="/w", orchestration=orchestration, isolation=isolation,
                                                      permissions=c.PermissionPolicy(mode=mode)))
                        self.assertFalse([a for a in argv if any(flag in a for flag in SKIP_FLAGS)], argv)
        bypass = argv_for(c.SessionSpec(cwd="/w", permissions=c.PermissionPolicy(mode="bypass")))
        self.assertNotIn("--dangerously-skip-permissions", bypass)

    def test_tool_policy_resume_fork_isolation_and_limits(self) -> None:
        spec = c.SessionSpec(
            cwd="/w", resume="old", fork=True, fork_at="msg-7", persist=False, isolation="isolated", thinking="off",
            output_schema={"type": "object"}, runs_detail="text", limits=c.CliLimits(max_usd=2.5, max_turns=4),
            tools=c.ToolPolicy(preset="read_only", allow=("shell:git status", "mcp:gh/list"), deny=("web_fetch",),
                               ask=("create",)))
        argv = argv_for(spec, native_id="new", resume="old")
        self.assertEqual(argv[argv.index("--resume"):argv.index("--resume") + 6],
                         ["--resume", "old", "--fork-session", "--session-id", "new", "--resume-session-at"])
        self.assertEqual(argv[argv.index("--tools"):argv.index("--tools") + 4], ["--tools", "Read", "Grep", "Glob"])
        self.assertEqual(argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")],
                         ["Bash(git status:*)", "mcp__gh__list"])
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "WebFetch")
        for flag in ("--strict-mcp-config", "--no-session-persistence", "--forward-subagent-text"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[argv.index("--thinking") + 1], "disabled")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.5")
        self.assertEqual(json.loads(argv[argv.index("--json-schema") + 1]), {"type": "object"})
        self.assertEqual(command.settings(spec, ("Read(~/.ssh/**)",)),
                         {"permissions": {"ask": ["Read(~/.ssh/**)", "Write"]}})
        none = argv_for(c.SessionSpec(cwd="/w", tools=c.ToolPolicy(preset="none")))
        self.assertEqual(none[none.index("--tools") + 1], "")

    def test_credential_ask_rules_cover_every_listed_path(self) -> None:
        rules = command.credential_ask_rules(Path("/home/u"), Path("/home/u/.quirq"),
                                             Path("/home/u/.gemini/antigravity-cli/antigravity-oauth-token"))
        for target in ("~/.ssh/**", "~/.aws/**", "~/.config/gh/**", "~/.claude/.credentials.json",
                       "~/.codex/auth.json", "~/.gemini/antigravity-cli/antigravity-oauth-token", "~/.quirq/**"):
            self.assertIn(f"Read({target})", rules)
            self.assertIn(f"Edit({target})", rules)
        outside = command.credential_ask_rules(Path("/home/u"), Path("/srv/state"), Path("/home/u/t"))
        self.assertIn("Read(//srv/state/**)", outside)

    def test_run_folder_and_files_are_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = c.SessionSpec(cwd="/w", system_prompt=c.SystemPrompt(text="x"),
                                 mcp_servers=(c.McpServer(name="s", transport="stdio", command=("srv", "-v")),))
            files = files_for(spec, Path(tmp) / "run" / "cse_1")
            self.assertEqual(stat.S_IMODE(files.folder.stat().st_mode), 0o700)
            for path in (files.system, files.mcp, files.settings):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
            self.assertEqual(json.loads(files.mcp.read_text())["mcpServers"]["s"],
                             {"type": "stdio", "command": "srv", "args": ["-v"], "env": {}})

    def test_run_files_never_follow_a_planted_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "run" / "cse_1"
            folder.mkdir(parents=True)
            target = Path(tmp) / "elsewhere"
            target.write_text("keep")
            (folder / "settings.json").symlink_to(target)
            with self.assertRaises(OSError):
                files_for(c.SessionSpec(cwd="/w"), folder)
            self.assertEqual(target.read_text(), "keep")

    def test_a_read_only_coordinator_keeps_its_orchestration_tools(self) -> None:
        spec = c.SessionSpec(cwd="/w", orchestration="coordinator", tools=c.ToolPolicy(preset="read_only"),
                             permissions=c.PermissionPolicy(mode="deny_unasked"))
        self.assertNotIn("--tools", argv_for(spec))
        self.assertEqual(support.check(spec).weakest("tools.preset").enforced_by, "host")
        plain = c.SessionSpec(cwd="/w", tools=c.ToolPolicy(preset="read_only"))
        self.assertIn("--tools", argv_for(plain))


class CheckAndFeaturesTests(unittest.TestCase):
    def test_check_is_truthful_and_refuses_what_it_cant_run(self) -> None:
        report = support.check(c.SessionSpec(cwd="/w", orchestration="coordinator",
                                             permissions=c.PermissionPolicy(mode="deny_unasked")))
        self.assertEqual(report.weakest("permissions.mode").enforced_by, "host")
        self.assertEqual(report.weakest("orchestration").enforced_by, "cli")
        for spec in (c.SessionSpec(cwd="/w", host_tools=(c.HostTool(name="t", description="", input_schema={}),)),
                     c.SessionSpec(cwd="/w", tools=c.ToolPolicy(deny=("delete",)),
                                   permissions=c.PermissionPolicy(mode="accept_edits")),
                     c.SessionSpec(cwd="/w", permissions=c.PermissionPolicy(sandbox="read_only")),
                     c.SessionSpec(cwd="/w", fork=True)):
            with self.subTest(spec=spec), self.assertRaises(c.SpecRefused):
                support.check(spec)
        with self.assertRaises(c.SpecRefused):
            support.check(c.SessionSpec(cwd="/w", strict=True, extensions={"claude_code.x": 1}))
        self.assertEqual(support.check(c.SessionSpec(cwd="/w", extensions={"x": 1})).weakest("extensions").support,
                         "ignored")

    #: ch 10 §4.2's workspace coordinator, as the preset is written.
    COORDINATOR_PRESET = dict(
        orchestration="coordinator", permissions=c.PermissionPolicy(mode="deny_unasked"),
        tools=c.ToolPolicy(preset="read_only",
                           allow=("read", "search", "list", "shell:git log", "shell:git show", "shell:ls",
                                  "mcp:xo_comms/*"),
                           deny=("edit", "create", "delete", "web_fetch", "web_search", "worktree", "schedule")))

    def test_the_designed_coordinator_preset_passes_check(self) -> None:
        report = support.check(c.SessionSpec(cwd="/w", **self.COORDINATOR_PRESET))
        for feature in ("orchestration", "permissions.mode", "tools.deny", "tools.allow", "tools.preset"):
            self.assertIn(report.weakest(feature).enforced_by, ("cli", "host"), feature)
        self.assertEqual((report.weakest("tools.deny").support, report.weakest("tools.deny").enforced_by),
                         ("emulated", "host"))

    def test_a_deny_claude_cant_express_is_the_hosts_only_where_the_host_sees_everything(self) -> None:
        deny = c.ToolPolicy(deny=("delete",))
        ask = support.check(c.SessionSpec(cwd="/w", tools=deny, permissions=c.PermissionPolicy(mode="ask")))
        self.assertEqual(ask.weakest("tools.deny").enforced_by, "host")
        for mode in ("accept_edits", "auto", "deny_unasked"):
            with self.subTest(mode), self.assertRaises(c.SpecRefused):
                support.check(c.SessionSpec(cwd="/w", tools=deny, permissions=c.PermissionPolicy(mode=mode)))

    def test_a_coordinator_is_refused_where_claude_would_act_unasked(self) -> None:
        for mode in ("accept_edits", "auto", "bypass"):
            with self.subTest(mode), self.assertRaises(c.SpecRefused) as caught:
                support.check(c.SessionSpec(cwd="/w", orchestration="coordinator",
                                            permissions=c.PermissionPolicy(mode=mode)))
            self.assertEqual(caught.exception.report.weakest("permissions.mode").support, "refused")
        for mode in ("ask", "plan", "deny_unasked"):
            with self.subTest(mode):
                support.check(c.SessionSpec(cwd="/w", orchestration="coordinator",
                                            permissions=c.PermissionPolicy(mode=mode)))

    def test_ask_mode_counts_on_the_host_for_a_delete_only_when_no_shell_is_allowed(self) -> None:
        for allow in (("shell",), ("shell:rm",), ("shell:git",), ("native:Bash",)):
            with self.subTest(allow), self.assertRaises(c.SpecRefused):
                support.check(c.SessionSpec(cwd="/w", permissions=c.PermissionPolicy(mode="ask"),
                                            tools=c.ToolPolicy(allow=allow, deny=("delete",))))
        coordinator = support.check(c.SessionSpec(cwd="/w", orchestration="coordinator",
                                                  permissions=c.PermissionPolicy(mode="deny_unasked"),
                                                  tools=c.ToolPolicy(allow=("shell:git log",), deny=("delete",))))
        self.assertEqual(coordinator.weakest("tools.deny").enforced_by, "host")   # its shell allows are stripped

    def test_feature_list(self) -> None:
        listed = support.features("2.1.301")
        self.assertEqual(listed.features["project_config_paths"].values, (".claude/**", ".mcp.json"))
        self.assertEqual(listed.features["runs.message"].support, "unsupported")
        self.assertEqual(listed.features["update.effort"].support, "emulated")
        self.assertEqual(listed.features["send_while_running"].behaviour, "steered")
        self.assertEqual(set(listed.features["permissions.mode"].values),
                         {"ask", "accept_edits", "plan", "deny_unasked", "auto", "bypass"})


class EnvTests(unittest.TestCase):
    SERVER = {
        "PATH": "/usr/bin", "HOME": "/home/u", "USER": "u", "LANG": "C.UTF-8", "LC_ALL": "C", "XDG_CONFIG_HOME": "/x",
        "HTTPS_PROXY": "http://proxy", "no_proxy": "localhost", "SSL_CERT_FILE": "/ca.pem", "NVM_DIR": "/nvm",
        "HOST": "127.0.0.1", "PORT": "5302", "CLAUDE_CONFIG_DIR": "/home/u/.claude",
        "ANTHROPIC_BASE_URL": "https://gw", "ANTHROPIC_AUTH_TOKEN": "gw-token",
        # never passed:
        "XO_API_KEY": "server-secret", "XO_AUTH_SESSION_ID": "sess", "XO_POLL_TOKEN": "poll",
        "JEV_API_KEY": "decision-model-key", "LEVANTO_API_KEY": "decision-model-key",
        "CLAUDE_CODE_MESSAGING_TOKEN": "parent-session", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CODEX_API_KEY": "x", "COMPOSIO_API_KEY": "server-only", "RANDOM_SERVER_SETTING": "x",
        # in the secrets store, so passed:
        "GITHUB_TOKEN": "from-store",
    }
    NEVER = ("XO_API_KEY", "XO_AUTH_SESSION_ID", "XO_POLL_TOKEN", "JEV_API_KEY", "LEVANTO_API_KEY",
             "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_API_KEY",
             "COMPOSIO_API_KEY", "RANDOM_SERVER_SETTING")

    def build(self, environ: dict[str, str], home: Path, *, store: dict[str, str] | None = None,
              switches: dict[str, str] | None = None) -> dict[str, str]:
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            return session_env(switches=switches or {}, spec_env={"XO_SESSION_TOKEN": "per-session"},
                               label="test", environ=environ, store=store if store is not None else
                               {"GITHUB_TOKEN": "stale-file-value", "STORE_ONLY": "from-file"}, home=home)

    def test_whitelist_passes_the_lists_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude.json").write_text(json.dumps({
                "mcpServers": {"gh": {"env": {"GH": "${GH_PAT}"}, "headers": {"A": "Bearer ${MCP_BEARER:-x}"}}},
                "projects": {"/p": {"mcpServers": {"db": {"args": ["--url", "${DB_URL}"]}}}}}))
            environ = {**self.SERVER, "GH_PAT": "p", "MCP_BEARER": "b", "DB_URL": "u"}
            env = self.build(environ, home, switches={"CLAUDE_CODE_COORDINATOR_MODE": "1"})
        for name in self.NEVER:
            self.assertNotIn(name, env)
        for name in ("PATH", "HOME", "LC_ALL", "XDG_CONFIG_HOME", "HTTPS_PROXY", "no_proxy", "SSL_CERT_FILE",
                     "NVM_DIR", "HOST", "PORT", "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                     "GH_PAT", "MCP_BEARER", "DB_URL"):
            self.assertEqual(env[name], environ[name], name)
        self.assertEqual(env["GITHUB_TOKEN"], "from-store")        # live value wins over the file's
        self.assertEqual(env["STORE_ONLY"], "from-file")
        self.assertEqual(env["XO_SESSION_TOKEN"], "per-session")
        self.assertEqual((env["DISABLE_AUTOUPDATER"], env["CLAUDE_CODE_SDK_READS_SESSION_STATE"]), ("1", "1"))
        self.assertEqual(env["CLAUDE_CODE_COORDINATOR_MODE"], "1")
        unexpected = set(env) - set(environ) - {"STORE_ONLY", "XO_SESSION_TOKEN", "DISABLE_AUTOUPDATER",
                                                 "CLAUDE_CODE_SDK_READS_SESSION_STATE", "CLAUDE_CODE_COORDINATOR_MODE"}
        self.assertEqual(unexpected, set())

    def test_stored_claude_code_keys_pass_but_inherited_ones_and_runtime_switches_dont(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self.build({"PATH": "/bin", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_USE_BEDROCK": "1"},
                             Path(tmp), store={"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000",
                                               "CLAUDE_CODE_COORDINATOR_MODE": "1", "MY_KEY": "x"})
        self.assertEqual((env["CLAUDE_CODE_USE_BEDROCK"], env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], env["MY_KEY"]),
                         ("1", "64000", "x"))
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", env)
        self.assertNotIn("CLAUDE_CODE_COORDINATOR_MODE", env)    # set only from the spec

    def test_decision_model_keys_never_pass_from_the_live_environ(self) -> None:
        """The B1 finding: ``load_dotenv()`` can load ``JEV_API_KEY`` and
        ``LEVANTO_API_KEY`` into the server's own environ. Values are never
        printed here."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"JEV_API_KEY": "x" * 8, "LEVANTO_API_KEY": "y" * 8, "HOME": tmp}):
            env = session_env(switches={}, spec_env={}, label="test", store={})
        self.assertFalse({"JEV_API_KEY", "LEVANTO_API_KEY"} & set(env))

    def test_auth_vars_follow_todays_function_exactly(self) -> None:
        """Every case of today's rules: the session gets exactly the auth
        variables ``cli_env()`` gives the providers probe."""
        auth = ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
        cases = [
            {"ANTHROPIC_API_KEY": "sk-ant-api03-x", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-y"},
            {"ANTHROPIC_API_KEY": "sk-ant-oat01-leaked", "ANTHROPIC_OAUTH_API_KEY": "sk-ant-none"},
            {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-y"},
            {},
        ]
        for login in (True, False):
            for case in cases:
                with self.subTest(login=login, case=sorted(case)), tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp)
                    if login:
                        (home / ".claude").mkdir()
                        (home / ".claude" / ".credentials.json").write_text(
                            json.dumps({"claudeAiOauth": {"refreshToken": "r"}}))
                    environ = {"PATH": "/usr/bin", "HOME": tmp, **case}
                    with mock.patch.dict(os.environ, environ, clear=True):
                        today = ClaudeCodeAdapter({}).cli_env()
                        env = session_env(switches={}, spec_env={}, label="test", store={})
                    self.assertEqual({k: env[k] for k in auth if k in env}, {k: today[k] for k in auth if k in today})


class FakeClaude:
    """A runtime session on the fake CLI, in a temp state root and home."""

    def __init__(self, steps: list[dict], *, argv: list[dict] | None = None) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = install("claude", self.root / "bin", scenario(steps, argv=argv or []))
        self.work = self.root / "work"
        self.work.mkdir()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CLI_PATH": str(self.fake.path), "HOME": str(self.home),
                                                "QUIRQ_STATE_ROOT": str(self.root / "state")})
        self.env.start()
        self.runtime = ClaudeRuntime(claude_home=self.home / ".claude")
        self.events: list[c.Event] = []
        self.reader: asyncio.Task | None = None

    async def open(self, host: ScriptedHost | None = None, *, consume: bool = True, **spec) -> c.Session:
        """``consume=False``: nobody reads ``events()`` (a stalled consumer)."""
        self.host = host or ScriptedHost(decide=lambda req: ALLOW)
        self.session = await self.runtime.open(c.SessionSpec(cwd=str(self.work), **spec), self.host,
                                               session_id="cse_test")
        if consume:
            self.reader = asyncio.create_task(self._consume())
        return self.session

    async def _consume(self) -> None:
        async for ev in self.session.events():
            self.events.append(ev)

    def of(self, kind: type) -> list:
        return [e for e in self.events if isinstance(e, kind)]

    async def finish(self) -> None:
        await asyncio.wait_for(self.session.close(), 20)
        if self.reader is not None:
            await asyncio.wait_for(self.reader, 20)
        self.env.stop()
        self.tmp.cleanup()

    def stdin(self, run: int = 0) -> list:
        return self.fake.invocations()[run]["stdin"]


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        if getattr(self, "cli", None) is not None:
            await self.cli.finish()

    def started(self, *after: dict) -> list[dict]:
        return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]

    async def test_open_writes_the_folder_and_close_removes_it(self) -> None:
        self.cli = FakeClaude(claude.initialize())
        await self.cli.open(system_prompt=c.SystemPrompt(text="role"))
        folder = self.cli.root / "state" / "run" / "cse_test"
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["settings.json", "system.txt"])
        run = self.cli.fake.invocations()[0]
        self.assertEqual(run["cwd"], str(self.cli.work))
        self.assertNotIn("XO_API_KEY", run["env"])
        await self.cli.session.close()
        self.assertFalse(folder.exists())

    async def test_an_ephemeral_session_leaves_no_project_folder(self) -> None:
        self.cli = FakeClaude(claude.initialize())
        await self.cli.open(persist=False)
        self.assertIn("--no-session-persistence", self.cli.fake.invocations()[0]["argv"])
        project = self.cli.home / ".claude" / "projects" / encode_path(os.path.realpath(self.cli.work))
        (project / "memory").mkdir(parents=True)        # what 2.1.301 leaves behind
        await self.cli.session.close()
        self.assertFalse(project.exists())

    def test_cleanup_never_removes_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "projects" / encode_path(os.path.realpath(tmp))
            (project / "memory").mkdir(parents=True)
            (project / "kept.jsonl").write_text("{}")
            remove_empty_project_folder(home, tmp)
            self.assertEqual(sorted(p.name for p in project.iterdir()), ["kept.jsonl"])

    async def test_the_prompt_and_context_go_over_stdin_never_argv(self) -> None:
        self.cli = FakeClaude(self.started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        await self.cli.open()
        turn = await self.cli.session.send(c.UserInput(text="secret plan", context="<wake reason=x/>", origin="wake"))
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished)
        message = self.cli.stdin()[1]["message"]
        self.assertEqual(message["content"], [{"type": "text", "text": "[XO context, not the person's words]\n"
                                                                       "<wake reason=x/>"},
                                              {"type": "text", "text": "secret plan"}])
        self.assertNotIn("secret plan", " ".join(self.cli.fake.invocations()[0]["argv"]))
        self.assertEqual([(e.turn_id, e.origin) for e in self.cli.of(c.TurnStarted)], [(turn, "wake")])

    async def test_host_error_is_a_deny(self) -> None:
        async def broken(req: c.PermissionRequest) -> c.PermissionDecision:
            raise RuntimeError("policy store down")

        self.cli = FakeClaude(self.started(
            claude.can_use_tool("p1", "Bash", {"command": "rm -rf build"}),
            expect({"type": "control_response", "response": {"request_id": "p1", "response": {
                "behavior": "deny", "message": "host error"}}}),
            *claude.text_turn(SID, "ok"), claude.idle(SID)))
        await self.cli.open(ScriptedHost(decide=broken))
        with self.assertLogs("services.cowork_agent.adapters.claude_code.runtime.session", "ERROR"):
            await self.cli.session.send(c.UserInput(text="clean"))
            await wait_for_event(self.cli.events, c.TurnFinished)
        resolved = self.cli.of(c.PermissionResolved)
        self.assertEqual([(e.decision.allow, e.decision.message) for e in resolved], [(False, "host error")])

    async def test_a_malformed_host_answer_is_a_deny(self) -> None:
        self.cli = FakeClaude(self.started(
            claude.can_use_tool("p1", "Bash", {"command": "make"}),
            expect({"type": "control_response", "response": {"request_id": "p1", "response": {
                "behavior": "deny", "message": "host error"}}}),
            *claude.text_turn(SID, "ok"), claude.idle(SID)))
        await self.cli.open(ScriptedHost(decide=lambda req: {"allow": True}))
        with self.assertLogs("services.cowork_agent.adapters.claude_code.runtime.session", "ERROR"):
            await self.cli.session.send(c.UserInput(text="make"))
            await wait_for_event(self.cli.events, c.TurnFinished)
        self.assertEqual([e.decision.allow for e in self.cli.of(c.PermissionResolved)], [False])

    async def test_open_refuses_a_missing_folder_and_leaves_nothing(self) -> None:
        self.cli = FakeClaude(claude.initialize())
        with self.assertRaises(c.RuntimeFailure) as caught:
            await self.cli.runtime.open(c.SessionSpec(cwd=str(self.cli.root / "gone")), ScriptedHost(),
                                        session_id="cse_x")
        self.assertEqual(caught.exception.code, "spec_refused")
        self.assertEqual(self.cli.fake.invocations(), [])
        self.cli.reader = None
        self.cli.session = await self.cli.open()

    async def test_open_fails_cleanly_when_claude_dies_during_initialize(self) -> None:
        self.cli = FakeClaude([{"stderr": "error: unknown option '--frobnicate'"}, {"exit": 1}])
        with self.assertRaises(c.RuntimeFailure) as caught:
            await self.cli.runtime.open(c.SessionSpec(cwd=str(self.cli.work)), ScriptedHost(), session_id="cse_x")
        self.assertEqual(caught.exception.code, "process_died")
        self.assertIn("unknown option", caught.exception.message)
        self.assertFalse((self.cli.root / "state" / "run" / "cse_x").exists())
        self.cli.env.stop()
        self.cli.tmp.cleanup()
        self.cli = None

    async def test_permission_request_is_neutral_and_names_the_sub_agent(self) -> None:
        self.cli = FakeClaude(self.started(
            claude.can_use_tool("p1", "Read", {"file_path": "notes/a.md"}, agent_id="task-7"),
            claude.permission_answer("p1", "allow"),
            claude.can_use_tool("p2", "WebFetch", {"url": "https://example.com/x", "prompt": "read"}),
            claude.permission_answer("p2", "deny"),
            claude.can_use_tool("p3", "mcp__gh__create_issue", {"title": "t"}),
            claude.permission_answer("p3", "allow"),
            *claude.text_turn(SID, "ok"), claude.idle(SID)))
        await self.cli.open(ScriptedHost(decide=lambda req: c.PermissionDecision(allow=req.kind != "web_fetch")))
        await self.cli.session.send(c.UserInput(text="go"))
        await wait_for_event(self.cli.events, c.TurnFinished)
        read, fetch, mcp = self.cli.host.permission_requests
        self.assertEqual((read.run, read.kind, read.paths, read.summary),
                         ("task-7", "read", (str(self.cli.work / "notes/a.md"),), f"read `{self.cli.work}/notes/a.md`"))
        self.assertEqual((fetch.kind, fetch.url, fetch.summary), ("web_fetch", "https://example.com/x",
                                                                  "fetch `https://example.com/x`"))
        self.assertEqual((mcp.kind, mcp.mcp), ("mcp", ("gh", "create_issue")))

    async def test_session_scope_is_remembered_here_never_written_to_claude(self) -> None:
        ask = claude.can_use_tool
        self.cli = FakeClaude(self.started(
            ask("p1", "Bash", {"command": "make"}), expect({"type": "control_response", "response": {
                "request_id": "p1", "response": {"behavior": "allow", "updatedInput": {"command": "make"}}}}),
            ask("p2", "Bash", {"command": "make"}), claude.permission_answer("p2", "allow"),
            *claude.text_turn(SID, "ok"), claude.idle(SID)))
        await self.cli.open(ScriptedHost(decide=lambda req: c.PermissionDecision(allow=True, scope="session")))
        await self.cli.session.send(c.UserInput(text="build twice"))
        await wait_for_event(self.cli.events, c.TurnFinished)
        self.assertEqual(len(self.cli.host.permission_requests), 1)
        answers = [line["response"]["response"] for line in self.cli.stdin() if line.get("type") == "control_response"]
        self.assertTrue(all("updatedPermissions" not in a for a in answers))
        self.assertEqual(len(self.cli.of(c.PermissionResolved)), 2)

    async def test_ask_user_question_goes_to_answer_question(self) -> None:
        self.cli = FakeClaude(self.started(
            claude.ask_user_question("q1", "Which colour?", ["red", "blue"]),
            expect({"type": "control_response", "response": {"request_id": "q1", "response": {
                "behavior": "allow", "updatedInput": {"answers": {"Which colour?": "blue"}}}}}),
            *claude.text_turn(SID, "blue it is"), claude.idle(SID)))
        await self.cli.open(ScriptedHost(answers={"Which colour?": "blue"}))
        await self.cli.session.send(c.UserInput(text="paint"))
        await wait_for_event(self.cli.events, c.TurnFinished)
        (question,) = self.cli.host.questions
        self.assertEqual(question.questions[0]["options"], [{"label": "red", "description": ""},
                                                            {"label": "blue", "description": ""}])
        self.assertEqual([e.answer for e in self.cli.of(c.QuestionAnswered)], [{"Which colour?": "blue"}])
        self.assertEqual(self.cli.of(c.PermissionRequested), [])

    async def test_withdrawn_request_cancels_the_hold(self) -> None:
        cancelled = asyncio.Event()

        async def hold(req: c.PermissionRequest) -> c.PermissionDecision:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.cli = FakeClaude(self.started(
            claude.can_use_tool("p1", "Bash", {"command": "deploy"}),
            {"sleep": 0.3}, {"emit": {"type": "control_cancel_request", "request_id": "p1"}},
            *claude.text_turn(SID, "ok"), claude.idle(SID)))
        await self.cli.open(ScriptedHost(decide=hold))
        await self.cli.session.send(c.UserInput(text="deploy"))
        await wait_for_event(self.cli.events, c.TurnFinished)
        await asyncio.wait_for(cancelled.wait(), 5)
        self.assertEqual([e.decision.message for e in self.cli.of(c.PermissionResolved)],
                         ["claude withdrew the request"])

    async def test_close_during_a_held_call_cancels_it_and_ends_cleanly(self) -> None:
        cancelled = asyncio.Event()

        async def hold(req: c.PermissionRequest) -> c.PermissionDecision:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        self.cli = FakeClaude(self.started(claude.tool_call(SID, "t1", "Bash", {"command": "deploy"}),
                                           claude.can_use_tool("p1", "Bash", {"command": "deploy"}, tool_use_id="t1"),
                                           claude.permission_answer("p1", "allow")))
        await self.cli.open(ScriptedHost(decide=hold))
        await self.cli.session.send(c.UserInput(text="deploy"))
        await wait_for_event(self.cli.events, c.PermissionRequested)
        pid = self.cli.host.launcher.spawned[0].pid
        await asyncio.wait_for(self.cli.session.close(), 10)
        await asyncio.wait_for(self.cli.reader, 10)
        self.assertTrue(cancelled.is_set())
        self.assertNotIn(pid, {p.pid for p in live_processes()})
        tail = [type(e).__name__ for e in self.cli.events[-3:]]
        self.assertEqual(tail, ["ToolFinished", "TurnFinished", "SessionClosed"])
        self.assertEqual(self.cli.of(c.ToolFinished)[0].status, "cancelled")
        self.assertEqual(self.cli.of(c.TurnFinished)[0].status, "interrupted")

    async def test_crash_mid_turn_is_events_not_an_exception(self) -> None:
        self.cli = FakeClaude(self.started(claude.task_started(SID, "task-9"),
                                           claude.tool_call(SID, "t1", "Bash", {"command": "make"}),
                                           {"stderr": "boom: out of memory"}, {"exit": 3}))
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="build"))
        await asyncio.wait_for(self.cli.reader, 10)
        names = [type(e).__name__ for e in self.cli.events]
        self.assertEqual(names[-5:], ["ToolFinished", "TurnFinished", "RunFinished", "Error", "SessionClosed"])
        self.assertEqual([(e.run, e.status) for e in self.cli.of(c.RunFinished)], [("task-9", "stopped")])
        error, closed = self.cli.events[-2], self.cli.events[-1]
        self.assertEqual((error.code, error.fatal), ("process_died", True))
        self.assertIn("boom: out of memory", error.message)
        self.assertEqual((closed.reason, closed.exit_code), ("process_died", 3))
        self.assertEqual(self.cli.of(c.TurnFinished)[0].status, "failed")
        with self.assertRaises(c.RuntimeFailure) as caught:
            await self.cli.session.send(c.UserInput(text="again"))
        self.assertEqual(caught.exception.code, "process_died")
        self.assertFalse((self.cli.root / "state" / "run" / "cse_test").exists())

    async def test_an_overlong_line_ends_the_session_as_a_protocol_error(self) -> None:
        self.cli = FakeClaude(self.started({"emit_raw": "x" * 4096}))
        with mock.patch("utils.commands.STREAM_LINE_LIMIT", 1024):
            await self.cli.open()
            await self.cli.session.send(c.UserInput(text="go"))
            await asyncio.wait_for(self.cli.reader, 10)
        errors = self.cli.of(c.Error)
        self.assertEqual([(e.code, e.fatal) for e in errors], [("protocol", True)])
        self.assertEqual(self.cli.of(c.SessionClosed)[0].reason, "protocol")

    async def test_interrupt_that_gets_no_answer_terminates_the_group(self) -> None:
        self.cli = FakeClaude(self.started(claude.tool_call(SID, "t1", "Bash", {"command": "sleep 100"}),
                                           expect({"type": "control_request", "request": {"subtype": "interrupt"}}),
                                           {"sleep": 60}))
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="sleep"))
        await wait_for_event(self.cli.events, c.ToolStarted)
        started = time.monotonic()
        await self.cli.session.interrupt(grace_s=1.0)
        # One deadline: escalated after grace_s, not after the control timeout and then grace_s again.
        self.assertLess(time.monotonic() - started, 1.7)
        await asyncio.wait_for(self.cli.reader, 10)
        self.assertEqual([e.code for e in self.cli.of(c.Notice)], ["interrupt", "interrupt_escalated"])
        self.assertEqual(self.cli.of(c.TurnFinished)[0].status, "interrupted")
        self.assertEqual(self.cli.of(c.SessionClosed)[0].reason, "interrupted")

    async def test_close_during_an_effort_restart_leaves_no_process(self) -> None:
        self.cli = FakeClaude([*claude.initialize(), {"sleep": 30}])
        await self.cli.open(effort="low")
        update = asyncio.create_task(self.cli.session.update(effort="high"))
        while self.cli.session._handle is not None:          # the restart has let go of the old process
            await asyncio.sleep(0)
        await asyncio.wait_for(self.cli.session.close(), 20)
        report = await asyncio.wait_for(update, 20)
        await asyncio.wait_for(self.cli.reader, 10)
        await asyncio.sleep(0.3)
        live = {p.pid for p in live_processes()}
        for handle in self.cli.host.launcher.spawned:
            self.assertNotIn(handle.pid, live)
            with self.assertRaises(ProcessLookupError):
                os.kill(handle.pid, 0)
        self.assertEqual(report.items[0].support, "refused")
        self.assertEqual(self.cli.of(c.SessionClosed)[0].reason, "closed")
        self.assertEqual(self.cli.session.state, "closed")
        self.assertFalse((self.cli.root / "state" / "run" / "cse_test").exists())

    async def test_close_during_an_effort_restart_with_a_stalled_consumer_never_hangs(self) -> None:
        # The queue fills while nobody reads; the restart then waits on the old
        # reader (blocked on the full queue) when close() cancels it.
        flood = [claude.rate_limit("allowed") for _ in range(EVENT_QUEUE_SIZE + 80)]
        self.cli = FakeClaude([*claude.initialize(), claude.user_message(), claude.init(SID),
                               *claude.text_turn(SID, "ok"), claude.idle(SID), *flood, {"sleep": 30}])
        await self.cli.open(effort="low", consume=False)
        await self.cli.session.send(c.UserInput(text="go"))
        while self.cli.session._queue.qsize() < EVENT_QUEUE_SIZE or self.cli.session._wire.current_turn:
            await asyncio.sleep(0.05)
        update = asyncio.create_task(self.cli.session.update(effort="high"))
        await asyncio.sleep(0.5)
        started = time.monotonic()
        await asyncio.wait_for(self.cli.session.close(), 15)
        self.assertLess(time.monotonic() - started, 10)
        report = await asyncio.wait_for(update, 10)
        self.assertEqual(report.items[0].support, "refused")
        self.assertEqual(len(self.cli.host.launcher.spawned), 1)        # no new claude after the cancel
        pid = self.cli.host.launcher.spawned[0].pid
        self.assertNotIn(pid, {p.pid for p in live_processes()})
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        restart = self.cli.session._restart_task
        self.assertTrue(restart is None or restart.done())
        self.cli.reader = asyncio.create_task(self.cli._consume())      # a consumer comes back: events() ends
        await asyncio.wait_for(self.cli.reader, 10)
        self.assertEqual(self.cli.of(c.SessionClosed)[0].reason, "closed")
        self.assertFalse((self.cli.root / "state" / "run" / "cse_test").exists())

    async def test_a_removed_run_folder_is_never_written_again(self) -> None:
        """A restart cancelled while its thread writes the run files must not
        bring the folder back after close() removed it."""
        self.cli = FakeClaude(claude.initialize())
        await self.cli.open()
        folder = self.cli.root / "state" / "run" / "cse_test"
        await self.cli.session.close()
        self.assertFalse(folder.exists())
        with self.assertRaises(c.RuntimeFailure):
            await asyncio.to_thread(self.cli.session._write_run_files)   # the late write of a cancelled restart
        self.assertFalse(folder.exists())

    async def test_concurrent_sends_during_a_pending_restart_both_go_through(self) -> None:
        self.cli = FakeClaude([*claude.initialize(), claude.user_message(), claude.init(SID),
                               *claude.text_turn(SID, "ok"), claude.idle(SID)])
        await self.cli.open(effort="low")
        self.cli.session._pending_effort = "high"            # as update(effort) leaves it mid-turn
        first, second = await asyncio.gather(self.cli.session.send(c.UserInput(text="one")),
                                             self.cli.session.send(c.UserInput(text="two")))
        self.assertEqual(first, second)                      # the second joins the turn the first started
        self.assertEqual(len(self.cli.fake.invocations()), 2)                  # one restart, not two
        self.assertEqual([e.code for e in self.cli.of(c.Notice)], ["restarted"])

    async def test_a_coordinator_cant_be_switched_to_a_mode_that_acts_unasked(self) -> None:
        self.cli = FakeClaude([*claude.initialize(), {"sleep": 30}])
        await self.cli.open(orchestration="coordinator", permissions=c.PermissionPolicy(mode="deny_unasked"))
        for mode in ("accept_edits", "auto"):
            report = await self.cli.session.update(permission_mode=mode)
            self.assertEqual(report.items[0].support, "refused", mode)
        sent = [line["request"]["subtype"] for line in self.cli.stdin() if line.get("type") == "control_request"]
        self.assertEqual(sent, ["initialize"])

    async def test_a_vanished_folder_is_spec_refused_not_a_missing_cli(self) -> None:
        self.cli = FakeClaude([*claude.initialize(), {"sleep": 30}])
        await self.cli.open()
        shutil.rmtree(self.cli.work)
        report = await self.cli.session.update(effort="high")
        self.assertEqual(report.items[0].support, "refused")
        self.assertIn("doesn't exist", report.items[0].note)
        await asyncio.wait_for(self.cli.reader, 10)
        self.assertEqual([e.code for e in self.cli.of(c.Error)], ["spec_refused"])
        self.cli.work.mkdir()

    async def test_runs_and_stop_task(self) -> None:
        self.cli = FakeClaude(self.started(
            claude.tool_call(SID, "toolu_task", "Agent", {"description": "reader", "subagent_type": "Explore"}),
            claude.task_started(SID, "task-1"), claude.task_progress(SID, "task-1", tokens=250, tool_uses=3),
            claude.tool_call(SID, "sub-1", "Read", {"file_path": "/w/a"}, agent_id="task-1", message_id="m2"),
            claude.tool_result(SID, "sub-1", "text", agent_id="task-1"),
            claude.tool_result(SID, "toolu_task", "launched"),
            *claude.text_turn(SID, "launched"), claude.idle(SID),
            *claude.host_control("stop_task"), *claude.task_killed(SID, "task-1"),
            claude.system(SID, "background_tasks_changed", tasks=[]),
            claude.init(SID), *claude.text_turn(SID, "it was stopped"), claude.idle(SID)))
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="read in the background"))
        await wait_for_event(self.cli.events, c.SessionIdle, after=c.TurnFinished)
        await self.cli.session.stop_run("task-1")
        await wait_for_event(self.cli.events, c.TurnFinished, count=2)
        (started,) = self.cli.of(c.RunStarted)
        self.assertEqual((started.run, started.info.kind, started.info.spawn_call_id, started.info.background),
                         ("task-1", "subagent", "toolu_task", True))
        (progress,) = self.cli.of(c.RunProgress)
        self.assertEqual((progress.run, progress.tokens, progress.tool_calls), ("task-1", 250, 3))
        self.assertEqual([(e.run, e.status) for e in self.cli.of(c.RunFinished)], [("task-1", "stopped")])
        self.assertEqual([e.run for e in self.cli.of(c.ToolStarted)], [None, "task-1"])
        self.assertEqual([e.origin for e in self.cli.of(c.TurnStarted)], ["person", "injected"])
        stop = [line for line in self.cli.stdin() if line.get("request", {}).get("subtype") == "stop_task"]
        self.assertEqual(stop[0]["request"]["task_id"], "task-1")
        with self.assertRaises(c.RuntimeFailure) as caught:
            await self.cli.session.send_to_run("task-1", c.UserInput(text="hi"))
        self.assertEqual(caught.exception.code, "unsupported")

    async def test_rate_limit_guard_refuses_send_until_the_reset(self) -> None:
        resets = time.time() + 3600
        self.cli = FakeClaude(self.started(claude.rate_limit("rejected", resets_at=resets, overage=True),
                                           *claude.text_turn(SID, "limited"), claude.idle(SID)))
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="go"))
        await wait_for_event(self.cli.events, c.TurnFinished)
        (limit,) = self.cli.of(c.RateLimit)
        self.assertEqual((limit.status, limit.window, limit.overage), ("limited", "five_hour", True))
        with self.assertRaises(c.RuntimeFailure) as caught:
            await self.cli.session.send(c.UserInput(text="again"))
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertAlmostEqual(caught.exception.retry_at.timestamp(), resets, delta=1)
        usage = await self.cli.runtime.usage()
        self.assertEqual((usage.overage, usage.windows[0].status), (True, "limited"))

    async def test_a_new_login_is_reported_at_the_turn_boundary(self) -> None:
        self.cli = FakeClaude(self.started(*claude.text_turn(SID, "one"), claude.idle(SID), claude.user_message(),
                                           *claude.text_turn(SID, "two"), claude.idle(SID)))
        creds = self.cli.home / ".claude" / ".credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "old", "accessToken": "a1"}}))
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="one"))
        await wait_for_event(self.cli.events, c.TurnFinished)
        creds.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "old", "accessToken": "a2"}}))
        self.assertEqual([e for e in self.cli.of(c.Notice) if e.code == "credentials_changed"], [])
        creds.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "new", "accessToken": "a3"}}))
        await self.cli.session.send(c.UserInput(text="two"))
        await wait_for_event(self.cli.events, c.TurnFinished, count=2)
        self.assertEqual(len([e for e in self.cli.of(c.Notice) if e.code == "credentials_changed"]), 1)

    async def test_update_model_and_mode_live_effort_by_restart(self) -> None:
        self.cli = FakeClaude([*claude.initialize(), *claude.host_control("set_model"),
                               *claude.host_control("set_permission_mode"),
                               {"sleep": 30}])
        await self.cli.open(orchestration="coordinator", permissions=c.PermissionPolicy(mode="deny_unasked"))
        report = await self.cli.session.update(model="sonnet", permission_mode="deny_unasked")
        self.assertEqual([(i.feature, i.support) for i in report.items],
                         [("update.model", "native"), ("update.permission_mode", "native")])
        sent = [line["request"] for line in self.cli.stdin() if line.get("type") == "control_request"]
        self.assertEqual(sent[1:], [{"subtype": "set_model", "model": "sonnet"},
                                    {"subtype": "set_permission_mode", "mode": "default"}])
        refused = await self.cli.session.update(permission_mode="bypass")
        self.assertEqual(refused.items[0].support, "refused")

    async def test_effort_restarts_the_process_and_resumes(self) -> None:
        self.cli = FakeClaude([*claude.initialize(), claude.user_message(), claude.init(SID),
                               *claude.text_turn(SID, "ok"), claude.idle(SID)])
        await self.cli.open(effort="low")
        native = self.cli.session.native_id
        report = await self.cli.session.update(effort="high")
        self.assertEqual(report.items[0].support, "emulated")
        await self.cli.session.send(c.UserInput(text="go"))
        await wait_for_event(self.cli.events, c.TurnFinished)
        first, second = self.cli.fake.invocations()[:2]
        self.assertEqual(first["argv"][first["argv"].index("--effort") + 1], "low")
        self.assertEqual(second["argv"][second["argv"].index("--effort") + 1], "high")
        self.assertEqual(second["argv"][second["argv"].index("--resume") + 1], native)
        self.assertEqual([e.code for e in self.cli.of(c.Notice)], ["restarted"])
        self.assertEqual(len(self.cli.of(c.SessionStarted)), 1)

    async def test_duplicate_requests_and_answers_are_ignored(self) -> None:
        release = asyncio.Event()

        async def decide(req: c.PermissionRequest) -> c.PermissionDecision:
            await release.wait()
            return ALLOW

        self.cli = FakeClaude(self.started(
            claude.can_use_tool("p1", "Bash", {"command": "make"}), claude.can_use_tool("p1", "Bash", {"command": "make"}),
            claude.permission_answer("p1", "allow"), *claude.text_turn(SID, "ok"), claude.idle(SID)))
        await self.cli.open(ScriptedHost(decide=decide))
        await self.cli.session.send(c.UserInput(text="go"))
        await wait_for_event(self.cli.events, c.PermissionRequested)
        await asyncio.sleep(0.3)
        release.set()
        await wait_for_event(self.cli.events, c.TurnFinished)
        self.assertEqual(len(self.cli.host.permission_requests), 1)

    async def test_stream_maps_text_thinking_and_file_bytes(self) -> None:
        def ev(event: dict) -> dict:
            return {"emit": {"type": "stream_event", "session_id": SID, "event": event}}

        content = 'line "one"\nnaïve'
        partial = json.dumps({"file_path": "/w/a.txt", "content": content})
        cut = len(partial) // 2
        self.cli = FakeClaude(self.started(
            ev({"type": "message_start", "message": {"id": "m1"}}),
            ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}),
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta",
                                                                        "thinking": "Check the file. Then more.\n\nPlan"}}),
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta",
                                                                        "thinking": " the edit."}}),
            ev({"type": "content_block_stop", "index": 0}),
            ev({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "w1",
                                                                               "name": "Write"}}),
            ev({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta",
                                                                        "partial_json": partial[:cut]}}),
            ev({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta",
                                                                        "partial_json": partial[cut:]}}),
            ev({"type": "content_block_stop", "index": 1}),
            {"emit": {"type": "assistant", "session_id": SID, "message": {"id": "m1", "content": [
                {"type": "thinking", "thinking": "Check the file. Then more.\n\nPlan the edit."},
                {"type": "tool_use", "id": "w1", "name": "Write", "input": {"file_path": "/w/a.txt",
                                                                            "content": content}}]}}},
            claude.tool_result(SID, "w1", "written"),
            claude.tool_call(SID, "td", "TodoWrite", {"todos": [{"content": "a", "status": "completed"},
                                                                {"content": "b", "status": "in_progress"}]}),
            claude.tool_result(SID, "td", "ok"),
            *claude.text_turn(SID, "done", message_id="m2"), claude.idle(SID)))
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="write"))
        await wait_for_event(self.cli.events, c.TurnFinished)
        self.assertEqual(self.cli.of(c.ThinkingDelta)[-1].step_title, "Plan the edit.")
        (thought,) = self.cli.of(c.ThinkingDone)
        self.assertEqual(thought.steps, ("Check the file.", "Plan the edit."))
        self.assertEqual(self.cli.of(c.ToolInputDelta)[-1].bytes_so_far, len(content.encode()))
        write = self.cli.of(c.ToolStarted)[0]
        self.assertEqual((write.kind, write.title, write.target), ("create_file", "Write `a.txt`", "/w/a.txt"))
        self.assertEqual(self.cli.of(c.ToolFinished)[0].size, len(content.encode()))
        (plan,) = self.cli.of(c.PlanUpdated)
        self.assertEqual([(i.text, i.state) for i in plan.items], [("a", "done"), ("b", "doing")])
        self.assertEqual([e.text for e in self.cli.of(c.TextDone)], ["done"])


class ContentCounterTests(unittest.TestCase):
    def test_counts_decoded_bytes_across_any_split(self) -> None:
        value = 'a"b\\c\né\U0001f600 end'
        raw = json.dumps({"file_path": "content", "content": value})
        for size in (1, 2, 3, 7, len(raw)):
            with self.subTest(size=size):
                counter = ContentCounter("content")
                for i in range(0, len(raw), size):
                    counter.feed(raw[i:i + size])
                self.assertEqual(counter.count, len(value.encode("utf-8")))


class TranscriptTests(unittest.IsolatedAsyncioTestCase):
    S1 = "11111111-1111-4111-8111-111111111111"
    S2 = "22222222-2222-4222-8222-222222222222"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".claude"
        shutil.copytree(FIXTURE_HOME, self.home)
        projects = self.home / "projects"
        os.utime(projects / "-work-app" / f"{self.S1}.jsonl", (1_760_000_200, 1_760_000_200))
        os.utime(projects / "-work-other" / f"{self.S2}.jsonl", (1_760_000_100, 1_760_000_100))
        self.runtime = ClaudeRuntime(claude_home=self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_list_sessions(self) -> None:
        sessions = await self.runtime.list_sessions()
        self.assertEqual([(s.native_id, s.cwd, s.title) for s in sessions], [
            (self.S1, "/work/app", "Fix the login bug"), (self.S2, "/work/other", "Release notes")])
        self.assertEqual(sessions[0].updated_at, "2025-10-09T08:56:40Z")
        only = await self.runtime.list_sessions(cwd="/work/other")
        self.assertEqual([s.native_id for s in only], [self.S2])
        self.assertEqual(await self.runtime.list_sessions(cwd="/nowhere"), [])

    async def test_read_session_pages_the_conversation(self) -> None:
        first = await self.runtime.read_session(self.S1, limit=2)
        self.assertEqual([i["id"] for i in first.items], ["u1", "a1"])
        rest = await self.runtime.read_session(self.S1, after=first.next, limit=10)
        self.assertEqual([i["id"] for i in rest.items], ["u2", "a2"])
        self.assertIsNone(rest.next)
        with self.assertRaises(c.RuntimeFailure) as caught:
            await self.runtime.read_session("33333333-0000-4000-8000-000000000000")
        self.assertEqual(caught.exception.code, "session_not_found")
        with self.assertRaises(c.RuntimeFailure):
            await self.runtime.read_session("../escape")

    async def test_the_cursor_is_a_byte_offset_and_a_stale_one_is_refused(self) -> None:
        path = self.home / "projects" / "-work-app" / f"{self.S1}.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        first = await self.runtime.read_session(self.S1, limit=2)
        self.assertEqual(first.next, str(len(lines[0]) + len(lines[1])))   # where line 3 starts
        for bad in ("5", "-1", str(path.stat().st_size + 1), "two", " 0", "0 ", "+0", "\u0661", "1e3"):
            with self.subTest(bad), self.assertRaises(c.RuntimeFailure) as caught:
                await self.runtime.read_session(self.S1, after=bad)
            self.assertIn("not a cursor", caught.exception.message)
        end = str(path.stat().st_size)
        self.assertEqual((await self.runtime.read_session(self.S1, after=end)).items, ())

    async def test_a_line_with_invalid_utf8_is_read_not_dropped(self) -> None:
        path = self.home / "projects" / "-work-app" / f"{self.S1}.jsonl"
        size = path.stat().st_size
        with path.open("ab") as f:
            f.write(b'{"type":"user","uuid":"u9","message":{"role":"user","content":"bad \xff byte"}}\n')
        page = await self.runtime.read_session(self.S1, after=str(size))
        self.assertEqual([(i["id"], i["message"]["content"]) for i in page.items], [("u9", "bad \ufffd byte")])

    async def test_a_line_claude_is_still_writing_waits_for_the_next_read(self) -> None:
        path = self.home / "projects" / "-work-app" / f"{self.S1}.jsonl"
        size = path.stat().st_size
        record = json.dumps({"type": "user", "uuid": "u3", "message": {"role": "user", "content": "more"}})
        with path.open("a") as f:
            f.write(record[:20])
        page = await self.runtime.read_session(self.S1, after=str(size))
        self.assertEqual(page.items, ())
        with path.open("a") as f:
            f.write(record[20:] + "\n")
        page = await self.runtime.read_session(self.S1, after=str(size))
        self.assertEqual([i["id"] for i in page.items], ["u3"])

    async def test_runs_from_the_sidecars(self) -> None:
        runs = await self.runtime.list_runs(self.S1)
        self.assertEqual([(r.run_id, r.parent_run_id, r.spawn_call_id, r.agent_type, r.title, r.background, r.depth)
                          for r in runs],
                         [("a1b2", None, "toolu_9", "Explore", "Find the login code", True, 1),
                          ("c3d4", "a1b2", "toolu_10", "worker", "Read the tests", False, 2)])
        page = await self.runtime.read_run(self.S1, "a1b2")
        self.assertEqual([(i["id"], i["agent_id"]) for i in page.items], [("r1", "a1b2"), ("r2", "a1b2")])
        with self.assertRaises(c.RuntimeFailure):
            await self.runtime.read_run(self.S1, "../../x")
        self.assertEqual(await self.runtime.list_runs(self.S2), [])


class ProbeAndOneshotTests(unittest.IsolatedAsyncioTestCase):
    def fake(self, steps: list[dict], argv: list[dict] | None = None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        fake = install("claude", root / "bin", scenario(steps, argv=argv or []))
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CLI_PATH": str(fake.path), "HOME": str(root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    async def test_describe_and_models_are_model_free(self) -> None:
        fake = self.fake(claude.initialize(), argv=[claude.version(), claude.auth_status(logged_in=True)])
        runtime = ClaudeRuntime()
        info = await runtime.describe()
        self.assertEqual((info.cli_version, info.logged_in, info.account), ("2.1.300", True, "claude.ai"))
        models = await runtime.models()
        self.assertEqual([(m.id, m.name, m.is_default) for m in models], [
            ("default", "Default (recommended)", True), ("haiku", "Haiku 5.5", False),
            ("claude-haiku-4-5-20251001", "Haiku 4.5", False)])
        self.assertEqual(models[1].efforts, ("low", "medium", "high", "xhigh", "max"))
        await runtime.models()                          # kept per installed binary: no second probe
        probes = [run for run in fake.invocations() if "--input-format" in run["argv"]]
        self.assertEqual(len(probes), 1)
        self.assertIn("--strict-mcp-config", probes[0]["argv"])     # none of the person's MCP servers start
        self.assertEqual(probes[0]["stdin"], [{"type": "control_request", "request_id": "probe",
                                               "request": {"subtype": "initialize"}}])
        self.assertEqual(runtime.features().cli_version, "2.1.300")

    async def test_oneshot(self) -> None:
        from services.cowork_agent.adapters.claude_code import oneshot_prompt
        from tests.fakes.host import Launcher

        fake = self.fake([claude.oneshot_result("A title")])
        result = await oneshot_prompt.oneshot().complete(
            c.OneshotRequest(prompt="Name this thread:\nfix login", model="haiku", effort="low"), Launcher())
        self.assertEqual((result.text, result.usage.input, result.usage.output), ("A title", 12, 3))
        run = fake.invocations()[0]
        self.assertEqual(run["argv"], ["--print", "--output-format", "json", "--tools", "", "--no-session-persistence",
                                       "--strict-mcp-config", "--model", "haiku", "--effort", "low"])
        self.assertEqual(run["stdin"], ["Name this thread:", "fix login"])
        self.assertTrue(Path(run["cwd"]).name.startswith("xo-oneshot-"))
        self.assertFalse(Path(run["cwd"]).exists())
        self.assertNotIn("XO_API_KEY", run["env"])
        isolation = oneshot_prompt.oneshot().check().weakest("isolation")
        self.assertEqual(isolation.support, "native")
        for word in ("user settings", "hooks", "user memory"):
            self.assertIn(word, isolation.note)

    async def test_oneshot_failures_are_runtime_failures(self) -> None:
        from services.cowork_agent.adapters.claude_code import oneshot_prompt
        from tests.fakes.host import Launcher

        self.fake([claude.oneshot_result("Not logged in", is_error=True)])
        with self.assertRaises(c.RuntimeFailure) as caught:
            await oneshot_prompt.oneshot().complete(c.OneshotRequest(prompt="x"), Launcher())
        self.assertEqual(caught.exception.code, "protocol")
        self.fake([{"sleep": 30}])
        launcher = Launcher()
        with self.assertRaises(c.RuntimeFailure) as caught:
            await oneshot_prompt.oneshot().complete(c.OneshotRequest(prompt="x", timeout_s=0.5), launcher)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertNotIn(launcher.spawned[0].pid, {p.pid for p in live_processes()})


class ModelsRouteTests(unittest.TestCase):
    def test_api_models_rows_keep_todays_shape(self) -> None:
        from services.cowork_agent.adapters.claude_code import models
        from services.cowork_agent.adapters.claude_code import runtime as runtime_pkg

        fresh = ClaudeRuntime()
        with tempfile.TemporaryDirectory() as tmp:
            fake = install("claude", Path(tmp) / "bin", scenario(claude.initialize()))
            with mock.patch.dict(os.environ, {"CLAUDE_CLI_PATH": str(fake.path), "HOME": tmp}), \
                    mock.patch.object(runtime_pkg, "_RUNTIME", fresh):
                rows = models.list_models()        # called the way the sync route calls it
        self.assertEqual([r["id"] for r in rows], ["claude_code/default", "claude_code/haiku",
                                                   "claude_code/claude-haiku-4-5-20251001"])
        for row in rows:
            self.assertEqual(set(row), {"id", "name", "provider_id", "capabilities", "pricing", "metadata"})
            self.assertEqual((row["provider_id"], row["pricing"]), ("claude_code", {"prompt": 0, "completion": 0}))
            self.assertIn("max_context", row["capabilities"])

    def test_api_models_lists_nothing_when_the_cli_is_missing(self) -> None:
        from services.cowork_agent.adapters.claude_code import models
        from services.cowork_agent.adapters.claude_code import runtime as runtime_pkg

        with mock.patch.dict(os.environ, {"CLAUDE_CLI_PATH": "/nonexistent/claude"}), \
                mock.patch.object(runtime_pkg, "_RUNTIME", ClaudeRuntime()), \
                self.assertLogs("services.cowork_agent.adapters.claude_code.models", "WARNING"):
            self.assertEqual(models.list_models(), [])


class ImportBoundaryTests(unittest.TestCase):
    def test_runtime_imports_no_fastapi_no_storm_no_other_agent(self) -> None:
        code = (
            "import sys\n"
            "import services.cowork_agent.adapters.claude_code.runtime\n"
            "import services.cowork_agent.adapters.claude_code.oneshot_prompt\n"
            "ours = ('claude_code', 'contract', 'loader', 'env_whitelist', 'cli_status', 'base')\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in ('fastapi', 'starlette')\n"
            "       or m.startswith('services.cowork_agent.storm')\n"
            "       or (m.startswith('services.cowork_agent.adapters.') and m.split('.')[3] not in ours)]\n"
            "print(bad)\n")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
