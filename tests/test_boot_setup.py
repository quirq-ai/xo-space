"""Boot setup: the named agent and each installed runtime run ``setup.sh``; only the named one may write .env."""

from __future__ import annotations

import unittest

from services.cowork_agent.registry.agent_registry import all_agents
from tests.frozen_sandbox import REPO, Sandbox

PROBE = """
import contextlib, io, json
from unittest import mock
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    import server
from services.cowork_agent.registry import agent_registry as reg
calls = []
def record(argv, **kw):
    calls.append([argv[-1].split("/")[-2], kw["env"]["XO_SETUP_OWNS_ENV"]])
    return mock.Mock(timed_out=False, binary_missing=False, exception=None, returncode=0)
first = reg.runtimes()[0]
with mock.patch.object(server, "run_sync", side_effect=record), \\
        mock.patch.object(reg, "installed", side_effect=lambda binary: binary == first.binary), \\
        contextlib.redirect_stdout(io.StringIO()):
    server._run_agent_setup()
print(json.dumps({"first": first.name, "calls": calls}))
"""


class BootSetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.exception = next(m.name for m in all_agents() if m.boot_rank is None
                             and (REPO / "config" / "agents" / m.name / "setup.sh").is_file())
        box = Sandbox()
        try:
            run = {"QUIRQ_SKIP_BOOT_INSTALL": "0"}
            cls.named = box.probe(PROBE, only=[cls.exception], env=run)[cls.exception]
            cls.none = box.probe(PROBE, only=[cls.exception], env={**run, "AGENT_NAME": ""})[cls.exception]
        finally:
            box.cleanup()

    def test_the_named_agent_owns_env_and_an_installed_runtime_does_not(self) -> None:
        self.assertEqual(self.named["calls"], [[self.exception, "1"], [self.named["first"], "0"]])

    def test_none_named_and_one_installed_writes_no_env(self) -> None:
        self.assertEqual(self.none["calls"], [[self.none["first"], "0"]])


if __name__ == "__main__":
    unittest.main()
