"""Every backend's agent is an xo-project its turns run in, as on Claude Code.

docs/session15sept/agent-backend-parity-audit.md §3.3 (F-S2, F-S3, F-S4):

- A chat in a project runs inside that project on every backend. hermes and
  openclaw have no per-request working directory, so each project gets the
  hermes profile / OpenClaw agent named like it, pointed at the project folder
  (hermes ``terminal.cwd``, openclaw entry ``cwd``) — also where each reads the
  project's ``AGENTS.md``. A chat with no project uses the default profile or
  agent.
- Creating an agent scaffolds its xo-project and writes ``.xo/agent.json``, and
  agents are listed from those records, as the CLI backends do. Native agents
  from before binding (no xo-project of that name) stay listed.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from services.cowork_agent import project_layout
from services.cowork_agent.adapters.claude_code import agents as claude_agents
from services.cowork_agent.adapters.hermes import adapter as hermes_adapter
from services.cowork_agent.adapters.hermes import agents as hermes_agents
from services.cowork_agent.adapters.hermes import project_binding as hermes_binding
from services.cowork_agent.adapters.openclaw import adapter as openclaw_adapter
from services.cowork_agent.adapters.openclaw import agents as openclaw_agents
from services.cowork_agent.adapters.openclaw import project_binding as openclaw_binding
from services.cowork_agent.adapters.openclaw import store as oc_store
from utils.commands import CommandResult


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        (self.base / "state").mkdir()
        (self.base / "projects").mkdir()
        env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.base / "state"),
            "XO_PROJECTS_ROOT": str(self.base / "projects"),
            "QUIRQ_RUNTIME_FILE": "",
            "QUIRQ_SECRETS_FILE": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def start(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value


def _record(name: str) -> dict:
    return json.loads((project_layout.xo_dir(name) / "agent.json").read_text())


def _body(name: str, **extra) -> SimpleNamespace:
    return SimpleNamespace(name=name, id=None, description="", workspace=None, **extra)


# ── hermes ────────────────────────────────────────────────────────────────────


class _FakeHermesCli:
    """``profile create`` makes the profile dir; ``config set terminal.cwd``
    writes it into the profile's config.yaml."""

    def __init__(self, profiles: Path) -> None:
        self.profiles = profiles
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail_on: str | None = None

    def __call__(self, argv, **kwargs):
        home = (kwargs.get("env") or {}).get("HERMES_HOME")
        self.calls.append((list(argv), home))
        if self.fail_on and self.fail_on in argv:
            return CommandResult(argv=list(argv), returncode=1, output="", stderr="boom", duration_seconds=0.0)
        if argv[1:3] == ["profile", "create"]:
            (self.profiles / argv[3]).mkdir(parents=True)
        elif argv[1:3] == ["config", "set"]:
            (Path(home) / "config.yaml").write_text(yaml.safe_dump({"terminal": {"cwd": argv[4]}}))
        return CommandResult(argv=list(argv), returncode=0, output="", duration_seconds=0.0)


class HermesBindingTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.profiles = self.base / "hermes" / "profiles"
        self.profiles.mkdir(parents=True)
        manifest = SimpleNamespace(agents_dir=self.profiles, binary="hermes", cwd=self.base, cli_timeout_seconds=5)
        self.start(patch.object(hermes_binding, "get_agent", return_value=manifest))
        self.start(patch.object(hermes_agents, "get_agent", return_value=manifest))
        self.cli = _FakeHermesCli(self.profiles)
        self.start(patch.object(hermes_binding, "run_sync", side_effect=self.cli))
        self.stopped: list[str] = []
        self.start(patch("services.cowork_agent.adapters.hermes.gateway_pool.stop_gateway", side_effect=self.stopped.append))

    def test_a_project_gets_a_profile_cloned_from_default_and_pointed_at_it(self) -> None:
        project = str(project_layout.project_dir("research"))
        self.assertEqual(hermes_binding.ensure_project_profile("research"), "research")
        self.assertEqual(self.cli.calls, [
            (["hermes", "profile", "create", "research", "--clone-from", "default"], None),
            (["hermes", "config", "set", "terminal.cwd", project], str(self.profiles / "research")),
        ])
        self.assertEqual(self.stopped, ["research"])

        self.cli.calls.clear()
        self.assertEqual(hermes_binding.ensure_project_profile("research"), "research")
        self.assertEqual(self.cli.calls, [])

    def test_an_existing_profile_is_repointed_at_its_project(self) -> None:
        (self.profiles / "research").mkdir()
        (self.profiles / "research" / "config.yaml").write_text(yaml.safe_dump({"terminal": {"cwd": "/elsewhere"}}))
        hermes_binding.ensure_project_profile("research")
        self.assertEqual([argv[1:3] for argv, _home in self.cli.calls], [["config", "set"]])
        self.assertEqual(hermes_binding.configured_cwd("research"), str(project_layout.project_dir("research")))

    def test_a_chat_without_a_project_uses_the_default_profile(self) -> None:
        self.assertIsNone(hermes_binding.ensure_project_profile(None))
        self.assertIsNone(hermes_binding.ensure_project_profile("default"))
        self.assertEqual(self.cli.calls, [])

    def test_a_new_turn_runs_in_its_projects_profile(self) -> None:
        with patch("services.cowork_agent.adapters.hermes.state_db.find_hermes_profile", return_value=None):
            self.assertEqual(hermes_adapter.HermesAdapter._resolve_profile("research", None), "research")
        with patch("services.cowork_agent.adapters.hermes.state_db.find_hermes_profile", return_value="other"):
            self.assertEqual(hermes_adapter.HermesAdapter._resolve_profile("research", "sid"), "other")

    def test_create_scaffolds_binds_and_records(self) -> None:
        with patch.object(hermes_agents, "_agent_info", side_effect=lambda name: {"name": name}):
            result = hermes_agents.create_agent(_body("Research"))
            self.assertEqual(result, {"name": "research"})
            self.assertTrue((project_layout.project_dir("research") / "AGENTS.md").is_file())
            self.assertEqual(_record("research")["backend"], "hermes")
            self.assertEqual(hermes_binding.configured_cwd("research"), str(project_layout.project_dir("research")))
            self.assertEqual(hermes_agents.create_agent(_body("Research")).status_code, 409)

    def test_create_adopts_a_profile_a_chat_already_made(self) -> None:
        hermes_binding.ensure_project_profile("research")
        self.cli.calls.clear()
        with patch.object(hermes_agents, "_agent_info", side_effect=lambda name: {"name": name}):
            self.assertEqual(hermes_agents.create_agent(_body("Research")), {"name": "research"})
        self.assertEqual(self.cli.calls, [])

    def test_a_failed_binding_is_reported(self) -> None:
        self.cli.fail_on = "terminal.cwd"
        result = hermes_agents.create_agent(_body("Research"))
        self.assertEqual(result.status_code, 500)
        self.assertIn("terminal.cwd", json.loads(result.body)["detail"])

    def test_agents_are_listed_from_records_plus_unbound_profiles(self) -> None:
        with patch.object(hermes_agents, "_agent_info", side_effect=lambda name: {"name": name}):
            hermes_agents.create_agent(_body("Research"))
            project_layout.project_dir("chatted").mkdir()
            with patch("services.cowork_agent.adapters.hermes.state_db.list_all_profile_names",
                       return_value=["default", "chatted", "old", "research"]):
                names = [a["name"] for a in hermes_agents.list_agents()]
        self.assertEqual(names, ["research", "default", "old"])


# ── openclaw ──────────────────────────────────────────────────────────────────


class _FakeOpenclawCli:
    """Applies ``agents add`` and ``config patch --stdin`` to the sandbox
    openclaw.json as the real CLI does. ``agents add`` refuses an existing id,
    names the identity after the id, creates the agent directory and stamps
    ``agents.ownership`` once the roster has more than one agent. ``config
    patch`` applies a JSON merge patch (``null`` deletes). Writing nothing, it
    refuses a config that is already invalid (an ``identity`` with keys outside
    OpenClaw's schema) with the CLI's invalid-config report, and a patch that
    would make it invalid with a validation error."""

    IDENTITY_KEYS = {"name", "theme", "emoji", "avatar"}

    def __init__(self, config: Path, agents_dir: Path) -> None:
        self.config = config
        self.agents_dir = agents_dir
        self.calls: list[list[str]] = []
        self.fail_on: str | None = None

    @staticmethod
    def _result(argv, returncode: int = 0, stderr: str = "") -> CommandResult:
        return CommandResult(argv=list(argv), returncode=returncode, output="", stderr=stderr, duration_seconds=0.0)

    @classmethod
    def _merge(cls, target: dict, changes: dict) -> None:
        for key, value in changes.items():
            if value is None:
                target.pop(key, None)
            elif isinstance(value, dict):
                if not isinstance(target.get(key), dict):
                    target[key] = {}
                cls._merge(target[key], value)
            else:
                target[key] = value

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if self.fail_on and self.fail_on in argv:
            return self._result(argv, 1, 'Config warnings: plugin not installed\n"research" is reserved.')
        cfg = json.loads(self.config.read_text())
        agents = cfg.setdefault("agents", {})
        entries = agents.setdefault("entries", {})
        if argv[1:3] == ["agents", "add"]:
            aid, workspace = argv[3], argv[argv.index("--workspace") + 1]
            if aid in entries:
                return self._result(argv, 1, f'Agent "{aid}" already exists.')
            entries[aid] = {"name": aid, "workspace": workspace, "identity": {"name": aid}}
            if len(entries) > 1:
                agents["ownership"] = "explicit"
            (self.agents_dir / aid / "agent").mkdir(parents=True, exist_ok=True)
        elif argv[1:4] == ["config", "patch", "--stdin"]:
            problem = self._identity_problem(cfg)
            if problem:
                return self._result(argv, 1, (
                    "OpenClaw config is invalid\nProblem:\n"
                    f"  - openclaw.json:1 — {problem}\n\n"
                    'Run "openclaw doctor --fix" to repair the config, then retry.'
                ))
            self._merge(cfg, json.loads(kwargs["input"]))
            problem = self._identity_problem(cfg)
            if problem:
                return self._result(argv, 1, f"Config validation failed: {problem}")
        self.config.write_text(json.dumps(cfg))
        return self._result(argv)

    @classmethod
    def _identity_problem(cls, cfg: dict) -> str | None:
        for aid, entry in cfg["agents"]["entries"].items():
            unknown = sorted(set(entry.get("identity") or {}) - cls.IDENTITY_KEYS)
            if unknown:
                return f'agents.entries.{aid}.identity: Unrecognized key: "{unknown[0]}"'
        return None


class OpenclawBindingTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.home = home = self.base / "openclaw"
        self.agents_dir = home / "agents"
        self.config = home / "openclaw.json"
        home.mkdir()
        self.config.write_text(json.dumps({"agents": {"entries": {"main": {}}}}))
        for target, name, value in (
            (oc_store, "OPENCLAW_JSON", self.config),
            (oc_store, "OPENCLAW_DIR", home),
            (oc_store, "DEFAULT_OPENCLAW_WORKSPACE", home / "workspace"),
            (openclaw_binding, "OPENCLAW_JSON", self.config),
            (openclaw_binding, "DEFAULT_OPENCLAW_WORKSPACE", home / "workspace"),
            (openclaw_agents, "AGENTS_DIR", self.agents_dir),
            (openclaw_agents, "DEFAULT_OPENCLAW_WORKSPACE", home / "workspace"),
            (openclaw_binding.cli, "_last_write", None),
        ):
            self.start(patch.object(target, name, value))
        self.cli = _FakeOpenclawCli(self.config, self.agents_dir)
        self.start(patch("services.cowork_agent.adapters.openclaw.cli.run_sync", side_effect=self.cli))

    def cfg(self) -> dict:
        return json.loads(self.config.read_text())

    def commands(self) -> list[list[str]]:
        return [call[1:3] for call in self.cli.calls]

    def test_a_project_gets_an_agent_that_runs_in_it(self) -> None:
        (self.home / "workspace").mkdir()
        (self.home / "workspace" / "SOUL.md").write_text("main soul")
        self.assertEqual(openclaw_binding.cli.seconds_until_applied(), 0.0)
        self.assertEqual(openclaw_binding.ensure_project_agent("research"), "research")
        agents = self.cfg()["agents"]
        entry = agents["entries"]["research"]
        workspace = (self.home / "workspace-research").resolve()
        self.assertEqual(entry["cwd"], str(project_layout.project_dir("research")))
        self.assertEqual(entry["workspace"], str(workspace))
        self.assertNotIn("identity", entry)
        self.assertEqual(agents["ownership"], "explicit")
        self.assertEqual((workspace / "SOUL.md").read_text(), "main soul")
        self.assertEqual(self.commands(), [["agents", "add"], ["config", "patch"]])
        # The first turn lets that last write reach the gateway.
        self.assertGreater(openclaw_binding.cli.seconds_until_applied(), 0.0)

        self.cli.calls.clear()
        openclaw_binding.ensure_project_agent("research")
        self.assertEqual(self.cli.calls, [])

    def test_an_existing_agent_is_repointed_and_keeps_its_settings(self) -> None:
        self.config.write_text(json.dumps({"agents": {"entries": {"research": {"model": "m", "cwd": "/elsewhere"}}}}))
        openclaw_binding.ensure_project_agent("research")
        entry = self.cfg()["agents"]["entries"]["research"]
        self.assertEqual((entry["model"], entry["cwd"]), ("m", str(project_layout.project_dir("research"))))
        self.assertEqual(self.commands(), [["config", "patch"]])

    def test_a_chat_without_a_project_uses_the_default_agent(self) -> None:
        self.assertEqual(openclaw_binding.ensure_project_agent(None), "main")
        self.assertEqual(openclaw_binding.ensure_project_agent("default"), "main")
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(openclaw_adapter.OpenclawAdapter._resolve_openclaw_agent("research"), "research")

    def test_a_chat_without_a_project_does_not_wait_for_the_config_lock(self) -> None:
        class _Held:
            def __enter__(self):
                raise AssertionError("took config_lock")

            def __exit__(self, *exc):
                return False

        with patch.object(openclaw_binding, "config_lock", _Held()):
            self.assertEqual(openclaw_binding.ensure_project_agent(None), "main")

    def test_a_failed_cli_call_is_reported_with_the_clis_reason(self) -> None:
        self.cli.fail_on = "add"
        with self.assertRaises(openclaw_binding.BindingError) as caught:
            openclaw_binding.ensure_project_agent("research")
        self.assertIn('"research" is reserved.', str(caught.exception))
        self.assertNotIn("research", self.cfg()["agents"]["entries"])

    def test_no_openclaw_config_is_an_error(self) -> None:
        self.config.unlink()
        with self.assertRaises(openclaw_binding.BindingError):
            openclaw_binding.ensure_project_agent("research")

    def test_create_scaffolds_binds_and_records(self) -> None:
        info = openclaw_agents.create_agent(_body("Research"))
        project = str(project_layout.project_dir("research"))
        self.assertEqual((info["name"], info["metadata"]["workspace"]), ("research", project))
        self.assertTrue((project_layout.project_dir("research") / "AGENTS.md").is_file())
        self.assertEqual(_record("research")["backend"], "openclaw")
        self.assertEqual(self.cfg()["agents"]["entries"]["research"]["cwd"], project)
        self.assertEqual(openclaw_agents.create_agent(_body("Research")).status_code, 409)

    def test_create_adopts_an_agent_a_chat_already_added(self) -> None:
        openclaw_binding.ensure_project_agent("research")
        info = openclaw_agents.create_agent(
            SimpleNamespace(name="Research Desk", id="research", description="", workspace=None)
        )
        self.assertEqual(info["name"], "research")
        self.assertEqual(self.cfg()["agents"]["entries"]["research"]["name"], "Research Desk")

    def test_agents_are_listed_from_records_plus_unbound_agents(self) -> None:
        openclaw_agents.create_agent(_body("Research"))
        openclaw_binding.ensure_project_agent("chatted")
        (self.agents_dir / "old").mkdir(parents=True)
        self.assertEqual([a["name"] for a in openclaw_agents.list_agents()], ["research", "old"])

    def test_patch_sets_and_clears_fields_through_the_cli(self) -> None:
        openclaw_agents.create_agent(_body("Research"))
        fields = dict(name="Research Desk", description="Finds things", model="openai/gpt-5.5",
                      workspace=None, identity_name=None, identity_emoji=None)
        openclaw_agents.patch("research", SimpleNamespace(model_fields_set={"name", "description", "model"}, **fields))
        entry = self.cfg()["agents"]["entries"]["research"]
        self.assertEqual((entry["name"], entry["description"], entry["model"]),
                         ("Research Desk", "Finds things", "openai/gpt-5.5"))
        self.assertEqual(_record("research")["name"], "Research Desk")
        self.assertEqual(openclaw_agents.get_detail("research")["description"], "Finds things")

        self.cli.calls.clear()
        cleared = {**fields, "name": None, "description": "", "model": ""}
        openclaw_agents.patch("research", SimpleNamespace(model_fields_set={"description", "model"}, **cleared))
        entry = self.cfg()["agents"]["entries"]["research"]
        self.assertNotIn("model", entry)
        self.assertNotIn("description", entry)
        self.assertEqual(self.commands(), [["config", "patch"]])

    def test_a_rejected_patch_changes_nothing_and_says_why(self) -> None:
        # An earlier XO release wrote descriptions as identity.bio, which makes
        # the whole config invalid for OpenClaw; the CLI refuses every write
        # until `openclaw doctor --fix`, and the error names the bad key.
        invalid = {"agents": {"entries": {"main": {}, "old": {"identity": {"bio": "legacy"}}}}}
        self.config.write_text(json.dumps(invalid))
        (self.agents_dir / "old").mkdir(parents=True)
        fields = dict(name="New", description=None, model="m", workspace=None, identity_name=None, identity_emoji="x")
        response = openclaw_agents.patch(
            "old", SimpleNamespace(model_fields_set={"name", "model", "identity_emoji"}, **fields)
        )
        self.assertEqual(response.status_code, 500)
        detail = json.loads(response.body)["detail"]
        self.assertIn('agents.entries.old.identity: Unrecognized key: "bio"', detail)
        self.assertIn('openclaw doctor --fix', detail)
        self.assertEqual(self.cfg(), invalid)

    def test_a_patch_that_would_invalidate_the_config_changes_nothing(self) -> None:
        openclaw_agents.create_agent(_body("Research"))
        before = self.cfg()
        with self.assertRaises(openclaw_binding.cli.OpenclawCliError) as caught:
            openclaw_binding.cli.patch({"agents": {"entries": {"research": {"identity": {"bio": "x"}}}}})
        self.assertIn('Config validation failed: agents.entries.research.identity: Unrecognized key: "bio"', str(caught.exception))
        self.assertEqual(self.cfg(), before)

    def test_create_adopts_an_entry_openclaw_has_not_run_yet(self) -> None:
        self.config.write_text(json.dumps({"agents": {"entries": {"main": {}, "hand": {"model": "m"}}}}))
        (self.home / "workspace").mkdir()
        (self.home / "workspace" / "SOUL.md").write_text("main soul")
        openclaw_agents.create_agent(_body("Hand"))
        self.assertTrue((self.agents_dir / "hand" / "agent").is_dir())
        self.assertEqual((self.home / "workspace-hand" / "SOUL.md").read_text(), "main soul")
        self.assertEqual(openclaw_agents.get_detail("hand")["display_name"], "Hand")
        self.assertEqual(self.cfg()["agents"]["entries"]["hand"]["model"], "m")
        self.assertEqual(self.commands(), [["config", "patch"]])


class CliAgentListingTests(_Sandbox):
    def test_a_cli_backend_lists_its_own_and_untagged_records(self) -> None:
        for name, backend in (("mine", "claude_code"), ("theirs", "codex"), ("old", None)):
            xo = project_layout.xo_dir(name)
            xo.mkdir(parents=True)
            record = {"id": name} if backend is None else {"id": name, "backend": backend}
            (xo / "agent.json").write_text(json.dumps(record))
        self.assertEqual([a["name"] for a in claude_agents.list_agents()], ["mine", "old"])
