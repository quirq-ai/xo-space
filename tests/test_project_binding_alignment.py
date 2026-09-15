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


class OpenclawBindingTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        home = self.base / "openclaw"
        self.agents_dir = home / "agents"
        self.config = home / "openclaw.json"
        home.mkdir()
        self.config.write_text(json.dumps({"agents": {"entries": {"main": {}}}}))
        for target, name, value in (
            (oc_store, "OPENCLAW_JSON", self.config),
            (oc_store, "OPENCLAW_DIR", home),
            (oc_store, "AGENTS_DIR", self.agents_dir),
            (oc_store, "DEFAULT_OPENCLAW_WORKSPACE", home / "workspace"),
            (openclaw_binding, "OPENCLAW_JSON", self.config),
            (openclaw_agents, "AGENTS_DIR", self.agents_dir),
        ):
            self.start(patch.object(target, name, value))

    def cfg(self) -> dict:
        return json.loads(self.config.read_text())

    def test_a_project_gets_an_agent_that_runs_in_it(self) -> None:
        self.assertEqual(openclaw_binding.ensure_project_agent("research"), "research")
        entry = self.cfg()["agents"]["entries"]["research"]
        self.assertEqual(entry["cwd"], str(project_layout.project_dir("research")))
        self.assertEqual(entry["workspace"], str((self.base / "openclaw" / "workspace-research").resolve()))
        self.assertTrue((self.agents_dir / "research" / "agent").is_dir())

        with patch.object(openclaw_binding, "write_openclaw_config") as write:
            openclaw_binding.ensure_project_agent("research")
        write.assert_not_called()

    def test_an_existing_agent_is_repointed_and_keeps_its_settings(self) -> None:
        self.config.write_text(json.dumps({"agents": {"entries": {"research": {"model": "m", "cwd": "/elsewhere"}}}}))
        openclaw_binding.ensure_project_agent("research")
        entry = self.cfg()["agents"]["entries"]["research"]
        self.assertEqual((entry["model"], entry["cwd"]), ("m", str(project_layout.project_dir("research"))))

    def test_a_chat_without_a_project_uses_the_default_agent(self) -> None:
        with patch.object(openclaw_binding, "write_openclaw_config") as write:
            self.assertEqual(openclaw_binding.ensure_project_agent(None), "main")
            self.assertEqual(openclaw_binding.ensure_project_agent("default"), "main")
        write.assert_not_called()
        self.assertEqual(openclaw_adapter.OpenclawAdapter._resolve_openclaw_agent("research"), "research")

    def test_a_legacy_list_config_gets_the_agent_without_cwd(self) -> None:
        self.config.write_text(json.dumps({"agents": {"list": [{"id": "main"}]}}))
        openclaw_binding.ensure_project_agent("research")
        entries = self.cfg()["agents"]["list"]
        self.assertEqual([e["id"] for e in entries], ["main", "research"])
        self.assertNotIn("cwd", entries[1])

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

    def test_cwd_is_not_written_into_a_legacy_list_config(self) -> None:
        entries = oc_store.apply_agent_entry({}, "main", "Main", Path("/w"), cwd=Path("/p"))
        self.assertEqual(entries["agents"]["entries"]["main"]["cwd"], "/p")
        legacy = oc_store.apply_agent_entry(
            {"agents": {"list": [{"id": "main"}]}}, "main", "Main", Path("/w"), cwd=Path("/p")
        )
        self.assertNotIn("cwd", legacy["agents"]["list"][0])


class CliAgentListingTests(_Sandbox):
    def test_a_cli_backend_lists_its_own_and_untagged_records(self) -> None:
        for name, backend in (("mine", "claude_code"), ("theirs", "codex"), ("old", None)):
            xo = project_layout.xo_dir(name)
            xo.mkdir(parents=True)
            record = {"id": name} if backend is None else {"id": name, "backend": backend}
            (xo / "agent.json").write_text(json.dumps(record))
        self.assertEqual([a["name"] for a in claude_agents.list_agents()], ["mine", "old"])
