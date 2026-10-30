"""Storm's presets (design ch 10): each preset's spec, read against a runtime's
feature list and checked by its runtime, never by its name.

The runtime here is claude's (the one B2 built): its real feature list, its
``check()`` with the engine's own ``requires`` rule, and the argv it builds.
A second, synthetic feature list stands for a runtime without a native auto
mode (plug and play: the preset switches on the list, ch 10 §5; §0.90).
"""

from __future__ import annotations

import dataclasses
import re
import unittest
from importlib import resources
from pathlib import Path

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import support
from services.cowork_agent.registry.agent_registry import all_agents
from services.cowork_agent.storm import composio, presets
from services.cowork_agent.storm.engine import runtime_providers
from services.cowork_agent.storm.engine.engine import _admit
from services.cowork_agent.storm.presets import Intelligence, PresetContext, PresetRefused
from services.cowork_agent.storm.review import Action, PolicyContext, SessionRef, decide
from services.cowork_agent.storm.review.paths import Places
from tests.test_claude_runtime import SKIP_FLAGS, argv_for
from tests.test_codex_runtime import SKIP_FLAGS as CODEX_SKIP_FLAGS, app_server_for

CLAUDE = support.features("2.1.301")
#: The same runtime, but without a native auto mode (as a runtime whose CLI holds it back).
NO_AUTO = dataclasses.replace(CLAUDE, features={k: v for k, v in CLAUDE.features.items() if k != "permissions.auto"})
#: Each runtime's own command-line builder and how its line says "bypass": a
#: runtime added without one fails the skip-flag test below (ch 15 §8: "the
#: three runtimes"). codex's line is its app-server's argv and the thread's
#: parameters (every session setting travels in ``thread/start``).
ARGV = {"claude_code": (argv_for, lambda argv: argv[argv.index("--permission-mode") + 1] == "bypassPermissions"),
        "codex": (app_server_for, lambda argv: '"approvalPolicy": "never"' in argv[-1])}
COMMS = c.McpServer(name="xo_comms", transport="http", url="http://127.0.0.1:5302/mcp/space-comms",
                    bearer_env="XO_COMMS_TOKEN", auto_approve=True, terminal_tools=("reply", "update_status"))


def ctx(preset: str, **kw) -> PresetContext:
    kw.setdefault("comms", COMMS)
    return PresetContext(preset=preset, agent=kw.pop("agent", "some_agent"), cwd=kw.pop("cwd", "/projects/app"), **kw)


def build(preset: str, features: c.FeatureList = CLAUDE, **kw) -> presets.PresetResult:
    return presets.build(ctx(preset, **kw), features)


class PresetSpecTests(unittest.TestCase):
    def test_every_preset_passes_the_runtimes_check_with_what_it_requires(self) -> None:
        cases = {"plain_chat": (), "workspace_coordinator": ("orchestration", "tools.deny", "permissions.mode"),
                 "project_coordinator": ("orchestration", "tools.deny", "permissions.mode"),
                 "thread": ("mcp_servers",), "job": ("mcp_servers",)}
        for name, requires in cases.items():
            with self.subTest(name):
                result = build(name)
                self.assertEqual(result.requires, requires)
                report = _admit(support, result.spec, result.requires, "some_agent")   # the engine's own rule
                for feature in requires:
                    self.assertIn(report.weakest(feature).enforced_by, ("cli", "host"), feature)

    def test_shared_defaults(self) -> None:
        for name in presets.PRESETS:
            with self.subTest(name):
                spec = build(name).spec
                self.assertEqual((spec.isolation, spec.thinking, spec.runs_detail, spec.persist, spec.fork,
                                  spec.strict, spec.limits), ("user", "summary", "steps", True, False, False,
                                                              c.CliLimits()))
                self.assertFalse(spec.env)                  # a preset never carries a token or any secret
                self.assertTrue(all(not s.env for s in spec.mcp_servers))

    def test_roles(self) -> None:
        self.assertEqual({n: build(n).role for n in presets.PRESETS},
                         {"plain_chat": "plain", "workspace_coordinator": "workspace-coordinator",
                          "project_coordinator": "project-coordinator", "thread": "thread", "job": "thread"})

    def test_coordinators_are_read_only_with_comms_their_only_server(self) -> None:
        for name in ("workspace_coordinator", "project_coordinator"):
            with self.subTest(name):
                spec = build(name).spec
                self.assertEqual((spec.orchestration, spec.permissions.mode, spec.tools.preset),
                                 ("coordinator", "deny_unasked", "read_only"))
                self.assertEqual(spec.tools.allow, ("read", "search", "list", "shell:git log", "shell:git show",
                                                    "shell:ls", "mcp:xo_comms/*"))
                self.assertEqual(spec.tools.deny, ("edit", "create", "delete", "web_fetch", "web_search",
                                                   "worktree", "schedule"))
                self.assertEqual(spec.mcp_servers, (COMMS,))
                self.assertEqual(spec.effort, "low")
                self.assertEqual(spec.system_prompt.mode, "append")

    def test_a_claude_coordinator_allows_exactly_the_read_set_and_comms(self) -> None:
        # §0.92: claude's own allow list, exactly as ch 02 §3.4 gives it; the
        # project coordinator's `git status` reaches XO's read-only check.
        for name in ("workspace_coordinator", "project_coordinator"):
            with self.subTest(name):
                argv = argv_for(build(name).spec)
                allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
                self.assertEqual(sorted(allowed), sorted(["Read", "Grep", "Glob", "LS", "Bash(git log:*)",
                                                          "Bash(git show:*)", "Bash(ls:*)", "mcp__xo_comms__*"]))

    def test_a_coordinators_git_status_is_allowed_by_xos_read_only_check(self) -> None:
        root = Path("/projects/app")
        places = Places(session_folder=root, home=Path("/home/x"), state_root=Path("/home/x/.quirq"), worktree=None,
                        project_folder=root, worktrees_root=root / ".xo" / "worktrees", git_common_dir=None,
                        cli_homes=(), project_config_paths=())
        coordinator = SessionRef(id="cse_c", role="coordinator", cwd=root, project_pid="app")
        verdict = decide(Action(kind="command", tool="Bash", session=coordinator, command="git status",
                                raw={"command": "git status"}), PolicyContext(places=places, now="2026-10-19T00:00:00Z"))
        self.assertEqual(verdict.effect, "allow")

    def test_workspace_and_project_coordinators_have_their_own_prompts(self) -> None:
        workspace, project = build("workspace_coordinator").spec, build("project_coordinator").spec
        self.assertIn("propose_project", workspace.system_prompt.text)
        self.assertIn("start_thread_session", project.system_prompt.text)
        self.assertNotEqual(workspace.system_prompt.text, project.system_prompt.text)

    def test_a_thread_keeps_every_tool_and_runs_auto_only_where_the_runtime_offers_it_natively(self) -> None:
        for name in ("thread", "job"):
            with self.subTest(name):
                native = build(name).spec
                self.assertEqual((native.permissions.mode, native.tools, native.orchestration, native.effort),
                                 ("auto", c.ToolPolicy(preset="standard"), "none", "high"))
                self.assertEqual(native.mcp_servers, (COMMS,))
                # A runtime without a native auto: XO's policy answers what the CLI asks (§0.90).
                self.assertEqual(build(name, NO_AUTO).spec.permissions.mode, "accept_edits")

    def test_a_job_run_is_a_thread(self) -> None:
        self.assertEqual(build("job").spec, build("thread").spec)

    def test_plain_chat_takes_the_requests_mode_auto_by_default(self) -> None:
        plain = build("plain_chat", comms=None).spec
        self.assertEqual((plain.permissions.mode, plain.system_prompt, plain.mcp_servers, plain.effort),
                         ("auto", None, (composio.marker(),), None))
        for mode in ("ask", "accept_edits", "plan", "deny_unasked"):
            with self.subTest(mode):
                self.assertEqual(build("plain_chat", request={"permission_mode": mode}).spec.permissions.mode, mode)
        with_comms = build("plain_chat", request={"comms": True}).spec
        self.assertEqual(with_comms.mcp_servers, (composio.marker(), COMMS))

    def test_plain_chat_mounts_composio_with_no_address_in_the_spec(self) -> None:
        """§0.108: the stored spec names Composio only; its URL (which holds
        the proxy token) is filled in when the process starts. Not comms: it
        is neither auto-approved nor ends a turn."""
        (server, *_rest) = build("plain_chat", comms=None).spec.mcp_servers
        self.assertEqual((server.name, server.transport, server.url, server.env, server.bearer_env,
                          server.auto_approve, server.terminal_tools),
                         ("composio", "http", None, {}, None, False, ()))

    def test_plain_chat_refuses_bypass_and_modes_the_runtime_lacks(self) -> None:
        narrow = dataclasses.replace(CLAUDE, features={**CLAUDE.features, "permissions.mode": c.FeatureSupport(
            support="native", values=("ask",))})
        for features, mode in ((CLAUDE, "bypass"), (narrow, "auto"), (CLAUDE, "yolo")):
            with self.subTest(mode), self.assertRaises(PresetRefused) as caught:
                presets.build(ctx("plain_chat", request={"permission_mode": mode} if mode != "auto" else {}),
                              features)
            self.assertEqual(caught.exception.code, "unsupported_permission_mode")
            self.assertNotIn("bypass", caught.exception.message.split("choose one of:")[1])

    def test_a_role_that_speaks_through_comms_needs_its_server(self) -> None:
        for name in ("workspace_coordinator", "project_coordinator", "thread", "job"):
            with self.subTest(name), self.assertRaises(PresetRefused):
                build(name, comms=None)
        with self.assertRaises(PresetRefused):
            build("plain_chat", comms=None, request={"comms": True})

    def test_pins_win_and_an_effort_the_runtime_lacks_is_left_to_the_cli(self) -> None:
        pinned = build("thread", intelligence=Intelligence(model="haiku", effort="low")).spec
        self.assertEqual((pinned.model, pinned.effort), ("haiku", "low"))
        no_effort = dataclasses.replace(CLAUDE, features={k: v for k, v in CLAUDE.features.items() if k != "effort"})
        self.assertIsNone(build("workspace_coordinator", no_effort).spec.effort)
        self.assertIsNone(build("thread", no_effort).spec.effort)


class PluggableTests(unittest.TestCase):
    def test_no_preset_branches_on_the_agent(self) -> None:
        for name in presets.PRESETS:
            with self.subTest(name):
                specs = [build(name, agent=m.name) for m in all_agents()]
                self.assertTrue(all(r == specs[0] for r in specs))

    def test_presets_skip_approvals_only_for_trusted_codex_auto(self) -> None:
        """Every preset and plain-chat mode keeps its approval checks, except
        trusted Codex auto. Explicit bypass requests are still refused."""
        self.assertEqual(sorted(ARGV), runtime_providers(), "a runtime without its argv builder here")
        modes = [m for m in CLAUDE.features["permissions.mode"].values if m != "bypass"]
        for runtime, (argv_of, bypassed) in ARGV.items():
            cases = [(name, {}) for name in presets.PRESETS] + \
                    [("plain_chat", {"permission_mode": m, "comms": comms}) for m in modes for comms in (False, True)]
            for name, request in cases:
                with self.subTest(runtime=runtime, preset=name, **request):
                    spec = build(name, request=request).spec
                    argv = argv_of(spec)
                    self.assertFalse([a for a in argv if any(flag in a for flag in (*SKIP_FLAGS, *CODEX_SKIP_FLAGS))],
                                     argv)
                    # Codex's trusted auto deliberately uses approvalPolicy never.
                    # Review modes and coordinators must continue to ask the host.
                    self.assertEqual(bypassed(argv), runtime == "codex" and spec.permissions.mode == "auto", argv)

    def test_the_prompts_name_no_cli_and_no_cli_tool(self) -> None:
        agents = [m.name for m in all_agents()]
        qualified = re.compile(r"mcp__|xo_comms\.|call_mcp_tool|view_file|spawn_agent|\b(Bash|Grep|Glob|WebFetch|TodoWrite)\b")
        for path in resources.files("services.cowork_agent.storm.presets").joinpath("prompts").iterdir():
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                self.assertFalse([a for a in agents if a.lower() in text.lower()])
                self.assertIsNone(qualified.search(text))


class FingerprintTests(unittest.TestCase):
    def test_it_moves_with_the_role_and_not_with_the_folder_pins_or_token(self) -> None:
        base = build("project_coordinator")
        same = build("project_coordinator", cwd="/elsewhere", intelligence=Intelligence(model="haiku", effort="high"),
                     comms=dataclasses.replace(COMMS, url="http://127.0.0.1:9/mcp", env={"XO_COMMS_TOKEN": "t"}))
        self.assertEqual(base.fingerprint, same.fingerprint)
        self.assertEqual(base.fingerprint, presets.fingerprint(base.spec))       # a stored spec compares
        self.assertNotEqual(base.fingerprint, build("workspace_coordinator").fingerprint)
        reworded = dataclasses.replace(base.spec, system_prompt=c.SystemPrompt(text="changed"))
        self.assertNotEqual(base.fingerprint, presets.fingerprint(reworded))
        retooled = dataclasses.replace(COMMS, terminal_tools=("reply",))
        self.assertNotEqual(base.fingerprint, build("project_coordinator", comms=retooled).fingerprint)


if __name__ == "__main__":
    unittest.main()
