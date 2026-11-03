"""What every storm runtime must offer, checked the same way for each
(mixed-runtime plan, step 4).

A project may run its coordinator on one runtime and its threads on
another, so every runtime must be able to run every role storm starts, with
what the role requires enforced by the CLI or by XO, never by the prompt
alone. Each runtime's own tests (``test_claude_runtime``,
``test_codex_runtime``, …) cover its wire against recorded CLI output; this
suite holds the contract all of them share. Runtimes are discovered, never
named: a new adapter is checked by adding it.
"""

from __future__ import annotations

import tempfile
import unittest
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import presets
from services.cowork_agent.storm.comms.tools import server_for
from services.cowork_agent.storm.engine import load_runtime, runtime_providers
from services.cowork_agent.storm.engine.engine import _admit
from services.cowork_agent.storm.presets import Intelligence, PresetContext
from services.cowork_agent.storm.presets.common import ROLES, thread_mode

#: Every feature storm reads from a runtime's feature list: each runtime lists
#: each one, as unsupported if it must, never leaves it out.
READ_FEATURES = ("effort", "interrupt", "runs.read", "runs.stop", "permissions.mode", "permissions.auto",
                 "mcp_servers", "orchestration", "update.permission_mode", "update.model", "update.effort",
                 "send_while_running", "input.images", "project_config_paths", "auth_env")
#: The presets a mixed project starts on any runtime, with their comms role.
PRESETS = ("thread", "job", "project_coordinator", "workspace_coordinator")


class ConformanceTests(unittest.TestCase):
    def runtimes(self) -> list[tuple[str, c.Runtime]]:
        found = [(name, rt) for name in runtime_providers() if (rt := load_runtime(name)) is not None]
        self.assertGreaterEqual(len(found), 2, "a mixed-runtime storm needs at least two runtimes to check")
        return found

    def test_each_runtime_lists_every_feature_storm_reads(self) -> None:
        for name, runtime in self.runtimes():
            with self.subTest(runtime=name):
                features = runtime.features()
                self.assertEqual(features.agent, name)
                self.assertTrue(features.contract_version)
                missing = [k for k in READ_FEATURES if k not in features.features]
                self.assertEqual(missing, [], f"{name} leaves out features storm reads")

    def test_each_runtime_can_run_every_role_a_mixed_project_starts(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            for name, runtime in self.runtimes():
                features = runtime.features()
                for preset in PRESETS:
                    with self.subTest(runtime=name, preset=preset):
                        result = presets.build(PresetContext(preset=preset, agent=name, cwd=cwd, project="demo",
                                                             intelligence=Intelligence(),
                                                             comms=server_for(ROLES[preset])), features)
                        report = _admit(runtime, result.spec, result.requires, name)   # raises SpecRefused
                        self.assertIsNotNone(report)

    def test_a_thread_runs_in_auto_or_with_xo_answering_on_every_runtime(self) -> None:
        for name, runtime in self.runtimes():
            with self.subTest(runtime=name):
                self.assertIn(thread_mode(runtime.features()), ("auto", "accept_edits"))

    def test_steering_and_interrupting_are_declared_so_a_coordinator_can_rely_on_them(self) -> None:
        for name, runtime in self.runtimes():
            with self.subTest(runtime=name):
                features = runtime.features().features
                interrupt = features["interrupt"]
                self.assertIn(interrupt.support, ("native", "emulated"), f"{name} can't interrupt a turn")
                steer = features["send_while_running"]
                # Steered, or queued for the next turn: either way a coordinator's message is never lost.
                self.assertIn(steer.support, ("native", "emulated", "unsupported"))


if __name__ == "__main__":
    unittest.main()
