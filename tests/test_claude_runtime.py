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
import dataclasses
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.adapter import ClaudeCodeAdapter
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime, command, support
from services.cowork_agent.adapters.claude_code.runtime.account import account_fingerprint
from services.cowork_agent.adapters.claude_code.runtime.env import session_env
from services.cowork_agent.adapters.claude_code.runtime.session import EVENT_QUEUE_SIZE
from services.cowork_agent.adapters.claude_code._project_encoding import encode_path
from services.cowork_agent.adapters.claude_code.runtime.transcript import remove_empty_project_folder
from services.cowork_agent.adapters.claude_code.runtime.tools import content_sha256
from services.cowork_agent.adapters.claude_code.runtime.wire import ContentCounter, Wire
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
        # ch 02 §3.4's allow list (owner, §0.92): the comms tools and the read set; the spec's
        # shell:git log / shell:ls map onto the same Bash(...) rules, listed once.
        self.assertEqual(allowed, ["Read", "Bash(git log:*)", "Bash(ls:*)", "mcp__xo_comms__*", "Grep", "Glob",
                                   "Bash(git show:*)"])
        self.assertEqual(command.settings(spec, ("Read(~/.ssh/**)",))["permissions"]["ask"],
                         ["Read(~/.ssh/**)"])
        switches = command.session_switches(spec)
        self.assertEqual(switches, {"CLAUDE_CODE_COORDINATOR_MODE": "1", "CLAUDE_CODE_MCP_PREWAIT_SERVERS": "xo_comms",
                                    "CLAUDE_CODE_TERMINAL_MCP_TOOLS": "mcp__xo_comms__reply", "XO_COMMS_TOKEN": "t"})
        self.assertEqual(command.mcp_config(spec), {"mcpServers": {
            "xo_comms": {"type": "http", "url": "http://x/mcp",
                         "headers": {"Authorization": "Bearer ${XO_COMMS_TOKEN}"}, "alwaysLoad": True,
                         "role": "comms"},
            "composio": {"type": "http", "url": "http://127.0.0.1:9/mcp"}}})
        self.assertIn(command.COORDINATOR_ADDENDUM, command.system_text(spec))
        bare = argv_for(c.SessionSpec(cwd="/w", orchestration="coordinator",
                                      permissions=c.PermissionPolicy(mode="deny_unasked")))
        self.assertEqual(bare[bare.index("--allowedTools") + 1:bare.index("--append-system-prompt-file")],
                         ["Read", "Grep", "Glob", "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)"])

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
        self.assertEqual(coordinator.weakest("tools.deny").enforced_by, "host")   # git log can't delete
        with self.assertRaises(c.SpecRefused):
            support.check(c.SessionSpec(cwd="/w", orchestration="coordinator",
                                        permissions=c.PermissionPolicy(mode="deny_unasked"),
                                        tools=c.ToolPolicy(allow=("shell",), deny=("delete",))))

    def test_feature_list(self) -> None:
        listed = support.features("2.1.301")
        self.assertEqual(listed.features["project_config_paths"].values, (".claude/**", ".mcp.json"))
        self.assertEqual(listed.features["runs.message"].support, "unsupported")
        self.assertEqual(listed.features["permissions.auto"].support, "native")    # C-auto1 passed (§0.90)
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

    def test_an_mcp_config_cannot_name_its_way_to_a_never_list_variable(self) -> None:
        """Cursor review: a ``${VAR}`` in ``~/.claude.json`` (which an agent
        can edit) doesn't reach a server secret, a decision-model key or a
        parent session's variable; an ordinary MCP variable still passes."""
        named = ("XO_API_KEY", "XO_AUTH_SESSION_ID", "XO_POLL_TOKEN", "XO_COMMS_TOKEN", "ROUTER_KEY",
                 "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDECODE", "CODEX_API_KEY", "GH_PAT")
        with tempfile.TemporaryDirectory() as tmp:
            home, state = Path(tmp) / "home", Path(tmp) / "state"
            home.mkdir()
            (state / "secrets").mkdir(parents=True)
            (state / "secrets" / "router.env").write_text("ROUTER_KEY=decision-model-key\n")
            (home / ".claude.json").write_text(json.dumps({"mcpServers": {
                "x": {"env": {name: "${%s}" % name for name in named}}}}))
            environ = {**self.SERVER, "XO_COMMS_TOKEN": "another-session", "ROUTER_KEY": "decision-model-key",
                       "GH_PAT": "p"}
            with mock.patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(state)}):
                env = self.build(environ, home)
        self.assertEqual(env["GH_PAT"], "p")
        self.assertEqual(env["XO_SESSION_TOKEN"], "per-session")       # the runtime's own token
        self.assertEqual(set(named) & set(env), {"GH_PAT"})

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

    async def test_a_sub_agents_ask_names_it_right_after_it_starts(self) -> None:
        """ch 14 §6: the request carries the run's title from ``task_started``."""
        self.cli = FakeClaude(self.started(
            claude.task_started(SID, "task-7", description="Check migrations"),
            claude.can_use_tool("p1", "Bash", {"command": "npm test"}, agent_id="task-7"),
            claude.permission_answer("p1", "allow"), *claude.text_turn(SID, "ok"), claude.idle(SID)))
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="go"))
        await wait_for_event(self.cli.events, c.TurnFinished)
        (req,) = self.cli.host.permission_requests
        self.assertEqual((req.run, req.run_title), ("task-7", "Check migrations"))

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
        # The hold is cancelled before the runtime records the withdrawal.
        await wait_for_event(self.cli.events, c.PermissionResolved)
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
        # The crash's Error comes first and names the turn it ended (a signal, not an order to infer from).
        self.assertEqual(names[-5:], ["Error", "ToolFinished", "TurnFinished", "RunFinished", "SessionClosed"])
        self.assertEqual(self.cli.events[-5].turn_id, self.cli.of(c.TurnFinished)[0].turn_id)
        self.assertEqual([(e.run, e.status) for e in self.cli.of(c.RunFinished)], [("task-9", "stopped")])
        error, closed = self.cli.events[-5], self.cli.events[-1]
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
        self.assertEqual([e.code for e in self.cli.of(c.Notice)], ["interrupt_escalated"])   # the escalation only
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
        # The turn ends only once both messages are in: a turn that ends
        # between the two sends rightly makes the second a turn of its own.
        self.cli = FakeClaude([*claude.initialize(), claude.user_message(), claude.init(SID), claude.user_message(),
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

    async def test_an_interrupt_records_the_text_and_thinking_it_cut_off(self) -> None:
        """What streamed before Stop survives: the open blocks are recorded as
        they stood, before the turn's end; a block with nothing in it isn't."""
        def ev(event: dict) -> dict:
            return {"emit": {"type": "stream_event", "session_id": SID, "event": event}}
        self.cli = FakeClaude(self.started(
            ev({"type": "message_start", "message": {"id": "m1"}}),
            ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}),
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta",
                                                                        "thinking": "Reading it.\n\nThen"}}),
            ev({"type": "message_start", "message": {"id": "m2"}}),
            ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Half a sen"}}),
            ev({"type": "content_block_start", "index": 1, "content_block": {"type": "text"}}),
            *claude.host_control("interrupt"),
            claude.result(SID, "", subtype="error_during_execution", terminal_reason="aborted_streaming"),
            claude.idle(SID)))
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="go"))
        await wait_for_event(self.cli.events, c.TextDelta)
        await self.cli.session.interrupt()
        await wait_for_event(self.cli.events, c.TurnFinished)
        (finished,) = self.cli.of(c.TurnFinished)
        self.assertEqual(finished.status, "interrupted")
        names = [type(e).__name__ for e in self.cli.events]
        self.assertEqual(names[names.index("ThinkingDone"):names.index("TurnFinished") + 1],
                         ["ThinkingDone", "TextDone", "Usage", "TurnFinished"])
        (thought,), (text,) = self.cli.of(c.ThinkingDone), self.cli.of(c.TextDone)
        self.assertEqual((thought.block_id, thought.steps), ("m1:0", ("Reading it.", "Then")))
        self.assertEqual((text.block_id, text.text, text.turn_id), ("m2:0", "Half a sen", finished.turn_id))

    async def test_a_close_mid_sentence_records_the_sentence_so_far(self) -> None:
        def ev(event: dict) -> dict:
            return {"emit": {"type": "stream_event", "session_id": SID, "event": event}}
        self.cli = FakeClaude(self.started(
            ev({"type": "message_start", "message": {"id": "m1"}}),
            ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "So far"}}),
            {"sleep": 60}))
        await self.cli.open()
        await self.cli.session.send(c.UserInput(text="go"))
        await wait_for_event(self.cli.events, c.TextDelta)
        await asyncio.wait_for(self.cli.session.close(), 10)
        await asyncio.wait_for(self.cli.reader, 10)
        self.assertEqual([type(e).__name__ for e in self.cli.events[-3:]], ["TextDone", "TurnFinished",
                                                                            "SessionClosed"])
        self.assertEqual(self.cli.of(c.TextDone)[0].text, "So far")


class AccountTests(unittest.IsolatedAsyncioTestCase):
    """The account a session runs on: the engine's pool is agent × account
    (ch 07 §5.1), so a reconnect to another account starts from a fresh pool."""

    def test_fingerprint_names_the_account_never_the_credential(self) -> None:
        """It follows the auth ``apply_auth`` leaves the CLI: with a usable
        native login the token variables are dropped, so claude's own account
        names it; a token variable names it only when it survives."""
        token = {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret"}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            claude_home = home / ".claude"
            claude_home.mkdir()
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                self.assertIsNone(account_fingerprint(claude_home, home, {}))           # logged out
                from_key = account_fingerprint(claude_home, home, token)               # token-only auth
                (claude_home / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"refreshToken": "rt-a"}}))
                from_login = account_fingerprint(claude_home, home, {})
                self.assertEqual(account_fingerprint(claude_home, home, token), from_login)   # the CLI ignores it
                (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-a"}}))
                from_account = account_fingerprint(claude_home, home, token)
                self.assertEqual(len({from_key, from_login, from_account}), 3)
                for label in (from_key, from_login, from_account):
                    self.assertRegex(label, r"^acct-[0-9a-f]{12}$")
                    self.assertNotIn("secret", label)
                # Connect Claude to account B with the pod's token still in the env: the account moves.
                (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "uuid-b"}}))
                self.assertNotEqual(account_fingerprint(claude_home, home, token), from_account)

    async def test_the_usage_guard_is_per_account(self) -> None:
        cli = FakeClaude([*claude.initialize(), claude.user_message(), claude.init(SID),
                          claude.rate_limit("rejected", resets_at=time.time() + 3600),
                          *claude.text_turn(SID, "limited"), claude.idle(SID)])
        self.addAsyncCleanup(cli.finish)
        config = cli.home / ".claude.json"
        config.write_text(json.dumps({"oauthAccount": {"accountUuid": "account-a"}}))
        await cli.open()
        await cli.session.send(c.UserInput(text="go"))
        await wait_for_event(cli.events, c.TurnFinished)
        self.assertEqual((await cli.runtime.usage()).windows[0].status, "limited")
        config.write_text(json.dumps({"oauthAccount": {"accountUuid": "account-b"}}))   # the person reconnected
        self.assertEqual((await cli.runtime.usage()).windows, ())


class ActionDigestTests(unittest.TestCase):
    """``PermissionRequest.content_sha256``: what defines a call, without
    what only describes it, so a grant matches claude's retry (ch 15 §6.3)."""

    def test_a_new_description_is_the_same_action_other_arguments_are_not(self) -> None:
        first = content_sha256("Bash", {"command": "git push origin main", "description": "Push the branch"})
        retry = content_sha256("Bash", {"command": "git push origin main", "description": "Push to origin"})
        other = content_sha256("Bash", {"command": "git push origin main", "description": "Push the branch",
                                        "run_in_background": True})
        self.assertEqual(first, retry)
        self.assertNotEqual(first, other)
        # The command is the neutral request's own field, hashed by the policy
        # in canonical form (other commands differ there), so it is left out
        # here, and a whitespace variant can match.
        self.assertEqual(content_sha256("Bash", {"command": "git push  origin main", "description": "x"}),
                         content_sha256("Bash", {"command": "git push origin dev", "description": "y"}))
        # A write's content and an MCP call's arguments define what it does.
        self.assertNotEqual(content_sha256("Write", {"file_path": "a", "content": "x"}),
                            content_sha256("Write", {"file_path": "a", "content": "y"}))
        self.assertNotEqual(content_sha256("mcp__gh__create_issue", {"title": "a"}),
                            content_sha256("mcp__gh__create_issue", {"title": "b"}))


class RunDepthTests(unittest.TestCase):
    def test_a_shell_without_a_depth_sits_one_below_its_parent(self) -> None:
        """Live run 4 (claude 2.1.304): a sub-agent's background shell comes
        with a parent and no spawn_depth."""
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        wire.start_turn("person")
        shell = {"type": "system", "subtype": "task_started", "session_id": SID, "task_id": "sh1",
                 "task_type": "local_bash", "parent_task_id": "a1", "description": "sleep"}
        outs = [o for line in (claude.task_started(SID, "a1", depth=2), shell) for o in wire.line(line["emit"]
                if "emit" in line else line)]
        depths = {o.fields["info"].run_id: o.fields["info"].depth for o in outs if o.cls is c.RunStarted}
        self.assertEqual(depths, {"a1": 2, "sh1": 3})


class IgnoredSystemLinesTests(unittest.TestCase):
    def test_commands_and_vcs_state_changes_are_known_and_carry_nothing(self) -> None:
        """Live (B3 run 1, claude 2.1.303/304): these were reported as
        unknown wire data."""
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        wire.start_turn("person")
        for subtype in ("commands_changed", "vcs_state_changed"):
            with self.subTest(subtype):
                self.assertEqual(wire.line({"type": "system", "subtype": subtype, "session_id": SID}), [])
        unknown = wire.line({"type": "system", "subtype": "brand_new", "session_id": SID})
        self.assertEqual([o.fields["code"] for o in unknown if o.cls is c.Notice], ["unknown_wire"])


class ApiFailureTests(unittest.TestCase):
    """An API failure claude reports in its result becomes a retryable
    ``Error`` before the failed ``TurnFinished`` (ch 07 §5.1)."""

    def outs(self, *lines: dict) -> list:
        wire = Wire(cwd="/w", runs_detail="steps", coordinator=False)
        wire.start_turn("person")
        return [out for line in lines for out in wire.line(line)]

    def result(self, **fields) -> dict:
        return {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "",
                "session_id": SID, "usage": {}, **fields}

    def failure(self, outs: list) -> tuple:
        errors = [o.fields for o in outs if o.cls is c.Error]
        turns = {o.fields["turn_id"] for o in outs if o.cls in (c.Error, c.TurnFinished)}
        self.assertEqual(len(turns), 1)                     # the error names its turn
        kinds = [o.cls.__name__ for o in outs if o.cls in (c.Error, c.TurnFinished)]
        return kinds, [(e["code"], e["retryable"], e["fatal"]) for e in errors]

    def test_an_interrupted_turn_has_no_error_and_its_zero_usage_is_unknown(self) -> None:
        """D3 (live run, claude 2.1.301): an interrupt's result carries
        claude's own diagnostic and all-zero usage."""
        zeros = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
        outs = self.outs(self.result(terminal_reason="aborted_streaming", total_cost_usd=0, usage=zeros,
                                     errors=["[ede_diagnostic] result_type=user last_content_type=n/a"]))
        (end,) = [o.fields for o in outs if o.cls is c.TurnFinished]
        self.assertEqual((end["status"], end["error"], end["usage"]), ("interrupted", None, None))
        self.assertEqual([o for o in outs if o.cls is c.Usage], [])
        # Usage it did report is kept; a failed turn still names its error.
        reported = self.outs(self.result(terminal_reason="aborted_streaming", total_cost_usd=0.002,
                                         usage={**zeros, "output_tokens": 140}))
        (end,) = [o.fields for o in reported if o.cls is c.TurnFinished]
        self.assertEqual((end["error"], end["usage"].output, end["usage"].cost_usd), (None, 140, 0.002))
        (end,) = [o.fields for o in self.outs(self.result(subtype="error_max_turns", errors=["Reached max turns"]))
                  if o.cls is c.TurnFinished]
        self.assertEqual((end["status"], end["error"]), ("failed", "Reached max turns"))

    def test_a_stop_keeps_what_streamed_even_when_its_usage_is_unknown(self) -> None:
        """Both rules of an interrupted turn at once: the cut-off text is
        recorded, and all-zero usage is unknown (no Usage event)."""
        def ev(event: dict) -> dict:
            return {"type": "stream_event", "session_id": SID, "event": event}
        zeros = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0}
        outs = self.outs(
            ev({"type": "message_start", "message": {"id": "m1"}}),
            ev({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
            ev({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Half a sen"}}),
            self.result(terminal_reason="aborted_streaming", total_cost_usd=0, usage=zeros))
        self.assertEqual([o.cls.__name__ for o in outs if o.cls in (c.TextDone, c.Usage, c.TurnFinished)],
                         ["TextDone", "TurnFinished"])
        (done,) = [o.fields for o in outs if o.cls is c.TextDone]
        (end,) = [o.fields for o in outs if o.cls is c.TurnFinished]
        self.assertEqual((done["text"], end["status"], end["usage"], end["error"]),
                         ("Half a sen", "interrupted", None, None))

    def test_overload_and_5xx_are_retryable(self) -> None:
        self.assertEqual(self.failure(self.outs(self.result(api_error_status=529))),
                         (["Error", "TurnFinished"], [("overloaded", True, False)]))
        self.assertEqual(self.failure(self.outs(self.result(api_error_status=503))),
                         (["Error", "TurnFinished"], [("api_error", True, False)]))
        self.assertEqual(self.failure(self.outs(self.result(errors=["Overloaded"]))),
                         (["Error", "TurnFinished"], [("overloaded", True, False)]))

    def test_retries_running_out_is_retryable_too(self) -> None:
        retry = {"type": "system", "subtype": "api_retry", "session_id": SID, "attempt": 10, "error_status": 500}
        self.assertEqual(self.failure(self.outs(retry, self.result())),
                         (["Error", "TurnFinished"], [("api_error", True, False)]))

    def test_a_retry_that_recovered_never_taints_a_later_failure(self) -> None:
        """The API answered after the api_retry (a message started): a later
        max-turns failure is the turn's own, not the API's."""
        retry = {"type": "system", "subtype": "api_retry", "session_id": SID, "attempt": 1, "error_status": 529}
        answered = {"type": "stream_event", "session_id": SID,
                    "event": {"type": "message_start", "message": {"id": "m1", "role": "assistant", "content": []}}}
        self.assertEqual(self.failure(self.outs(retry, answered, self.result(subtype="error_max_turns"))),
                         (["TurnFinished"], []))
        self.assertEqual(self.failure(self.outs(retry, answered, self.result())), (["TurnFinished"], []))
        # A max-turns result is never the API's, even right after an unanswered retry.
        self.assertEqual(self.failure(self.outs(retry, self.result(subtype="error_max_turns"))), (["TurnFinished"], []))

    def test_other_failures_are_not(self) -> None:
        self.assertEqual(self.failure(self.outs(self.result(api_error_status=400))), (["TurnFinished"], []))
        self.assertEqual(self.failure(self.outs(self.result())), (["TurnFinished"], []))



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
        self.assertEqual([(i.id, i.type, i.text) for i in first.items],
                         [("u1", "input", "Fix the login bug\nin app.py"), ("a1", "text", "Looking at it.")])
        rest = await self.runtime.read_session(self.S1, after=first.next, limit=10)
        self.assertEqual([(i.id, i.type, i.call_id, i.text) for i in rest.items],
                         [("u2", "tool_result", "toolu_1", "ok"), ("a2", "text", None, "Fixed.")])
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
            self.assertIn("is not a place in transcript", caught.exception.message)
        end = str(path.stat().st_size)
        self.assertEqual((await self.runtime.read_session(self.S1, after=end)).items, ())

    async def test_a_line_with_invalid_utf8_is_read_not_dropped(self) -> None:
        path = self.home / "projects" / "-work-app" / f"{self.S1}.jsonl"
        size = path.stat().st_size
        with path.open("ab") as f:
            f.write(b'{"type":"user","uuid":"u9","message":{"role":"user","content":"bad \xff byte"}}\n')
        page = await self.runtime.read_session(self.S1, after=str(size))
        self.assertEqual([(i.id, i.text) for i in page.items], [("u9", "bad \ufffd byte")])

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
        self.assertEqual([i.id for i in page.items], ["u3"])

    async def test_runs_from_the_sidecars(self) -> None:
        runs = await self.runtime.list_runs(self.S1)
        self.assertEqual([(r.run_id, r.parent_run_id, r.spawn_call_id, r.agent_type, r.title, r.background, r.depth)
                          for r in runs],
                         [("a1b2", None, "toolu_9", "Explore", "Find the login code", True, 1),
                          ("c3d4", "a1b2", "toolu_10", "worker", "Read the tests", False, 2),
                          ("f5e6", None, "toolu_k1", "fork", "Try the other fix", True, 1)])
        with self.assertRaises(c.RuntimeFailure):
            await self.runtime.read_run(self.S1, "../../x")
        self.assertEqual(await self.runtime.list_runs(self.S2), [])

    async def test_a_run_reads_as_neutral_steps(self) -> None:
        """claude's records as the contract's items (§0.110): a block each,
        tools in neutral terms, a denial and an interruption told apart, an
        output claude kept elsewhere marked cut with its whole size (claude's
        count, else its envelope's) and only its preview as the text, an
        image part named, a Write that replaced its file an edit, messages
        from the coordinator and a peer as input, an API failure as an
        error, and claude's other meta records and attachments left out."""
        page = await self.runtime.read_run(self.S1, "a1b2", limit=100)
        self.assertIsNone(page.next)
        item = c.TranscriptItem
        self.assertEqual(page.items, (
            item(id="r1", type="input", ts="2026-10-15T10:00:02Z", text="Find the login code"),
            item(id="r2", type="thinking", ts="2026-10-15T10:00:03Z",
                 text="Look for the login handler first. It is probably in src.\n\nThen check its tests.",
                 steps=("Look for the login handler first.", "Then check its tests."), duration_ms=1830),
            item(id="r3", type="tool_call", ts="2026-10-15T10:00:04Z", call_id="toolu_g1", tool="Grep", kind="search",
                 title="Search for `def login`", target="def login"),
            item(id="r4", type="tool_result", ts="2026-10-15T10:00:04Z", call_id="toolu_g1", status="ok",
                 text="src/auth.py:12:def login(user, password):"),
            item(id="r5", type="tool_call", ts="2026-10-15T10:00:05Z", call_id="toolu_b1", tool="Bash", kind="command",
                 title="Run the auth tests", target="pytest -q tests/test_auth.py"),
            item(id="r6", type="tool_result", ts="2026-10-15T10:00:09Z", call_id="toolu_b1", status="ok",
                 text="." * 1900 + "\nShell cwd was reset to /work/app", bytes=40960, truncated=True),
            # The file exists by the time anyone reads this; the call wrote the whole file.
            item(id="r7", type="tool_call", ts="2026-10-15T10:00:10Z", call_id="toolu_w1", tool="Write",
                 kind="create_file", title="Write `notes.md`", target="/work/app/notes.md"),
            item(id="r8", type="tool_result", ts="2026-10-15T10:00:11Z", call_id="toolu_w1", status="denied",
                 text="The user doesn't want to proceed with this tool use. The tool use was rejected."),
            item(id="r9", type="tool_call", ts="2026-10-15T10:00:12Z", call_id="toolu_t1", tool="TodoWrite",
                 kind="plan", title="Update the plan",
                 plan=(c.PlanItem(text="Read auth.py", state="done"),
                       c.PlanItem(text="Report the login code", state="doing"))),
            item(id="r10", type="tool_result", ts="2026-10-15T10:00:12Z", call_id="toolu_t1", status="ok",
                 text="Todos have been modified successfully."),
            item(id="r11", type="tool_call", ts="2026-10-15T10:00:13Z", call_id="toolu_r1", tool="Read", kind="read",
                 title="Read `login.png`", target="/work/app/login.png"),
            item(id="r12", type="tool_result", ts="2026-10-15T10:00:13Z", call_id="toolu_r1", status="ok",
                 text="The login form\n[image]"),
            item(id="r13", type="tool_call", ts="2026-10-15T10:00:14Z", call_id="toolu_b2", tool="Bash",
                 kind="command", title="Serve the app", target="python3 -m http.server"),
            item(id="r14", type="tool_result", ts="2026-10-15T10:00:20Z", call_id="toolu_b2", status="cancelled",
                 text="[Request interrupted by user for tool use]"),
            item(id="r14:1", type="input", ts="2026-10-15T10:00:20Z", text="Stop that and report what you found."),
            item(id="m3", type="input", ts="2026-10-15T10:00:21Z", text="Also check the logout path."),
            item(id="m4", type="input", ts="2026-10-15T10:00:21Z", text="auth.py is mine; don't edit it."),
            item(id="r16", type="tool_call", ts="2026-10-15T10:00:22Z", call_id="toolu_f1", tool="WebFetch",
                 kind="web_fetch", title="Read `example.com`", target="https://example.com/auth"),
            # No toolUseResult: the size is the envelope's 61.5 KB.
            item(id="r17", type="tool_result", ts="2026-10-15T10:00:24Z", call_id="toolu_f1", status="ok",
                 text="# Auth\nLogin posts to /session.", bytes=62976, truncated=True),
            item(id="r18", type="tool_call", ts="2026-10-15T10:00:25Z", call_id="toolu_w2", tool="Write",
                 kind="edit_file", title="Write `README.md`", target="/work/app/README.md"),
            item(id="r19", type="tool_result", ts="2026-10-15T10:00:25Z", call_id="toolu_w2", status="ok",
                 text="The file README.md has been updated."),
            item(id="r20", type="thinking", ts="2026-10-15T10:00:26Z"),
            item(id="r21", type="error", ts="2026-10-15T10:00:27Z",
                 text="API Error: 529 Overloaded. Try again shortly."),
            item(id="r15", type="text", ts="2026-10-15T10:00:21Z", text="In auth.py."),
        ))
        self.assertEqual(json.loads(json.dumps(dataclasses.asdict(page.items[8])))["plan"],
                         [{"text": "Read auth.py", "state": "done"},
                          {"text": "Report the login code", "state": "doing"}])

    async def test_a_depth_2_run_without_times(self) -> None:
        page = await self.runtime.read_run(self.S1, "c3d4")
        self.assertEqual(page, c.TranscriptPage(items=(c.TranscriptItem(id="n1", type="input", text="nested"),)))

    async def test_a_record_s_steps_are_never_split_between_pages(self) -> None:
        whole = (await self.runtime.read_run(self.S1, "a1b2", limit=100)).items
        pages, after = [], None
        while True:
            page = await self.runtime.read_run(self.S1, "a1b2", after=after, limit=1)
            pages.append([i.id for i in page.items])
            if (after := page.next) is None:
                break
        self.assertEqual([i for ids in pages for i in ids], [i.id for i in whole])
        self.assertIn(["r14", "r14:1"], pages)
        # The Write's result is on the next page; its page still reads it as the edit it was.
        (write,) = [i for i in whole if i.id == "r18"]
        page = await self.runtime.read_run(self.S1, "a1b2", after=await self.cursor_before("a1b2", "r18"), limit=1)
        self.assertEqual(([i.id for i in page.items], page.items[0].kind, write.kind), (["r18"], "edit_file",
                                                                                        "edit_file"))
        self.assertTrue(all(len(ids) == 1 for ids in pages if ids != ["r14", "r14:1"]))

    async def cursor_before(self, run: str, item_id: str) -> str | None:
        """The cursor of the page whose first item is ``item_id``."""
        after = None
        while True:
            page = await self.runtime.read_run(self.S1, run, after=after, limit=1)
            if page.items[0].id == item_id:
                return after
            after = page.next

    async def test_a_fork_shows_its_own_steps_not_the_history_it_inherited(self) -> None:
        """A forked run starts with its parent's history and claude's
        instructions to the fork: it shows only its directive and what it did
        after, and no cursor points into what it inherited."""
        page = await self.runtime.read_run(self.S1, "f5e6")
        self.assertEqual(page, c.TranscriptPage(items=(
            c.TranscriptItem(id="f1:1", type="input", ts="2026-10-15T10:00:01Z",
                             text="Try the token refresh fix instead."),
            c.TranscriptItem(id="f2", type="text", ts="2026-10-15T10:00:03Z", text="Done: refresh works."))))
        first = await self.runtime.read_run(self.S1, "f5e6", limit=1)
        self.assertEqual([i.id for i in first.items], ["f1:1"])
        rest = await self.runtime.read_run(self.S1, "f5e6", after=first.next)
        self.assertEqual(([i.id for i in rest.items], rest.next), (["f2"], None))
        path = self.home / "projects" / "-work-app" / self.S1 / "subagents" / "agent-f5e6.jsonl"
        lines = path.read_bytes().splitlines(keepends=True)
        for inherited in (0, len(lines[0]), len(lines[0]) + len(lines[1])):
            with self.subTest(inherited), self.assertRaises(c.RuntimeFailure) as caught:
                await self.runtime.read_run(self.S1, "f5e6", after=str(inherited))
            self.assertEqual(caught.exception.code, "protocol")

    async def test_a_run_not_marked_a_fork_shows_every_step(self) -> None:
        meta = self.home / "projects" / "-work-app" / self.S1 / "subagents" / "agent-f5e6.meta.json"
        meta.write_text(json.dumps({**json.loads(meta.read_text()), "isFork": False}))
        page = await self.runtime.read_run(self.S1, "f5e6")
        self.assertEqual([(i.id, i.type) for i in page.items],
                         [("p1", "input"), ("p2", "tool_call"), ("f1:1", "input"), ("f2", "text")])

    async def test_a_write_read_back_is_a_create_though_its_file_exists_by_then(self) -> None:
        path = self.home / "projects" / "-work-app" / f"{self.S1}.jsonl"
        written = Path(self.tmp.name) / "app.py"
        written.write_text("print(1)\n")
        size = path.stat().st_size
        with path.open("a") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "a8", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_8", "name": "Write",
                 "input": {"file_path": str(written), "content": "print(1)\n"}}]}}) + "\n")
        (call,) = (await self.runtime.read_session(self.S1, after=str(size))).items
        self.assertEqual((call.kind, call.target), ("create_file", str(written)))

    async def test_a_step_of_no_known_kind_is_reported_by_name(self) -> None:
        path = self.home / "projects" / "-work-app" / f"{self.S1}.jsonl"
        size = path.stat().st_size
        with path.open("a") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "a9", "timestamp": "not a time", "message": {
                "role": "assistant", "content": [{"type": "server_tool_use", "id": "s1"}, "torn",
                                                 {"type": "redacted_thinking", "data": "x"}]}}) + "\n")
        page = await self.runtime.read_session(self.S1, after=str(size))
        self.assertEqual(page.items, (c.TranscriptItem(id="a9", type="other", text="server_tool_use"),
                                      c.TranscriptItem(id="a9:1", type="other", text="unreadable block"),
                                      c.TranscriptItem(id="a9:2", type="thinking")))


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
        (Path(os.environ["HOME"]) / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": "u-1"}}))
        runtime = ClaudeRuntime()
        info = await runtime.describe()
        expected = "acct-" + hashlib.sha256(b"oauth:u-1").hexdigest()[:12]
        self.assertEqual((info.cli_version, info.logged_in, info.account), ("2.1.300", True, expected))
        await runtime.describe()                        # the probes are kept per binary and login
        self.assertEqual(sum(1 for run in fake.invocations() if run["argv"] == ["--version"]), 1)
        models = await runtime.models()
        self.assertEqual([(m.id, m.name, m.is_default) for m in models], [
            ("default", "Default (recommended)", True), ("haiku", "Haiku 5.5", False),
            ("claude-haiku-4-5-20251001", "Haiku 4.5", False)])
        self.assertEqual(models[1].efforts, ("low", "medium", "high", "xhigh", "max"))
        # 2.1.301 says per model whether it supports effort: Haiku 4.5 doesn't.
        self.assertEqual([m.takes_effort for m in models], [True, True, False])
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


class ModelRowsTests(unittest.TestCase):
    def test_a_claude_that_never_says_lets_every_model_take_effort(self) -> None:
        from services.cowork_agent.adapters.claude_code.runtime import _models
        rows = [{"value": "a"}, {"value": "b", "supportedEffortLevels": ["low"]}]
        self.assertEqual([(m.takes_effort, m.efforts) for m in _models(rows)], [(True, ()), (True, ("low",))])
        said = [{"value": "a", "supportsEffort": False}, {"value": "b", "supportedEffortLevels": ["low"]},
                {"value": "c", "supportsEffort": True}]
        self.assertEqual([m.takes_effort for m in _models(said)], [False, True, True])


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


    def test_api_models_lists_nothing_when_the_cli_isnt_executable(self) -> None:
        from services.cowork_agent.adapters.claude_code import models
        from services.cowork_agent.adapters.claude_code import runtime as runtime_pkg

        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "claude"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o644)                    # present, not executable: PermissionError, was a 500
            with mock.patch.dict(os.environ, {"CLAUDE_CLI_PATH": str(binary), "HOME": tmp}), \
                    mock.patch.object(runtime_pkg, "_RUNTIME", ClaudeRuntime()), \
                    self.assertLogs("services.cowork_agent.adapters.claude_code.models", "WARNING") as logs:
                self.assertEqual(models.list_models(), [])
        self.assertIn("cli_missing", logs.output[0])


class ModelsRouteConcurrencyTests(unittest.TestCase):
    """/api/models as FastAPI runs it: several worker threads, each with its
    own event loop, calling at once on a cold cache."""

    def call_three(self, steps: list[dict]) -> tuple[list, int]:
        from services.cowork_agent.adapters.claude_code import models
        from services.cowork_agent.adapters.claude_code import runtime as runtime_pkg

        with tempfile.TemporaryDirectory() as tmp:
            fake = install("claude", Path(tmp) / "bin", scenario(steps))
            out: list = [None] * 3

            def call(i: int) -> None:
                out[i] = models.list_models()

            with mock.patch.dict(os.environ, {"CLAUDE_CLI_PATH": str(fake.path), "HOME": tmp}), \
                    mock.patch.object(runtime_pkg, "_RUNTIME", ClaudeRuntime()), \
                    mock.patch.object(runtime_pkg.probe, "INITIALIZE_PROBE_TIMEOUT_S", 1.0):
                threads = [threading.Thread(target=call, args=(i,)) for i in range(3)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(20)
            return out, len(fake.invocations())

    def test_three_threads_on_a_slow_healthy_cli_all_get_the_models_from_one_spawn(self) -> None:
        out, spawns = self.call_three([{"sleep": 0.5}, *claude.initialize()])
        self.assertEqual([[r["id"] for r in rows] for rows in out], [["claude_code/default", "claude_code/haiku",
                                                                     "claude_code/claude-haiku-4-5-20251001"]] * 3)
        self.assertEqual(spawns, 1)

    def test_three_threads_on_a_hung_cli_all_get_nothing_from_one_spawn(self) -> None:
        with self.assertLogs("services.cowork_agent.adapters.claude_code.models", "WARNING"):
            out, spawns = self.call_three([{"sleep": 30}])
        self.assertEqual(out, [[], [], []])
        self.assertEqual(spawns, 1)


class ModelsProbeFailureTests(unittest.IsolatedAsyncioTestCase):
    """A CLI that can't answer costs one probe per ``MODELS_FAILURE_TTL_S``,
    never one per /api/models call."""

    TIMEOUT = "services.cowork_agent.adapters.claude_code.runtime.probe.INITIALIZE_PROBE_TIMEOUT_S"
    ANSWERING_TIMEOUT_S = 30.0      # far past a fake's start under load

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.fake = install("claude", self.root / "bin", scenario([{"sleep": 30}]))   # never answers initialize
        (self.root / ".claude").mkdir()
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CLI_PATH": str(self.fake.path), "HOME": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        timeout = mock.patch(self.TIMEOUT, 0.5)
        timeout.start()
        self.addCleanup(timeout.stop)
        self.runtime = ClaudeRuntime(claude_home=self.root / ".claude")

    def probes(self) -> int:
        return len(self.fake.invocations())

    def answering(self):
        """For a CLI that does answer: the 0.5 s is for the hung one, and a
        fake's start under load can take that long by itself."""
        return mock.patch(self.TIMEOUT, self.ANSWERING_TIMEOUT_S)

    async def failure(self) -> c.RuntimeFailure:
        with self.assertRaises(c.RuntimeFailure) as caught:
            await self.runtime.models()
        return caught.exception

    async def test_a_hung_cli_is_asked_once_then_answered_from_memory(self) -> None:
        self.assertEqual((await self.failure()).code, "timeout")
        started = time.monotonic()
        again = await self.failure()
        self.assertLess(time.monotonic() - started, 0.3)       # no second 0.5 s wait
        self.assertEqual(again.code, "timeout")
        self.assertEqual(self.probes(), 1)                      # and no second process

    async def test_a_changed_binary_or_login_asks_again_at_once(self) -> None:
        await self.failure()
        os.utime(self.fake.path, ns=(1, 1))                     # the CLI was reinstalled
        await self.failure()
        self.assertEqual(self.probes(), 2)
        (self.root / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {"refreshToken": "new"}}')
        await self.failure()
        self.assertEqual(self.probes(), 3)

    async def test_the_failure_is_forgotten_after_its_ttl(self) -> None:
        await self.failure()
        with mock.patch("services.cowork_agent.adapters.claude_code.runtime.MODELS_FAILURE_TTL_S", 0):
            await self.failure()
        self.assertEqual(self.probes(), 2)

    async def test_concurrent_calls_share_one_hung_probe_and_its_failure(self) -> None:
        started = time.monotonic()
        results = await asyncio.gather(*(self.runtime.models() for _ in range(3)), return_exceptions=True)
        self.assertLess(time.monotonic() - started, 2.0)       # one 0.5 s timeout, not three
        self.assertEqual([r.code for r in results], ["timeout"] * 3)
        self.assertEqual(self.probes(), 1)

    async def test_concurrent_calls_share_one_slow_healthy_probe(self) -> None:
        slow = install("claude", self.root / "bin", scenario([{"sleep": 0.3}, *claude.initialize()]))
        with self.answering():
            results = await asyncio.gather(*(self.runtime.models() for _ in range(3)))
        self.assertEqual([len(r) for r in results], [3, 3, 3])
        self.assertEqual(len(slow.invocations()), 1)

    async def test_a_success_is_kept_per_binary_and_login(self) -> None:
        good = install("claude", self.root / "bin", scenario(claude.initialize()))
        with self.answering():
            self.assertEqual(len(await self.runtime.models()), 3)
            await self.runtime.models()
        self.assertEqual(len(good.invocations()), 1)


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
