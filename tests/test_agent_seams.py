"""The two seams (plan 4.2): only ``space_agent()`` and ``default_runtime()``
read AGENT_NAME, DEFAULT_AGENT and the Space's preferred runtime, every call,
and an exception Space keeps its own agent whatever runtime threads prefer."""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.loader import list_capability_providers, try_load_capability
from services.cowork_agent.registry import agent_registry as reg

REPO = Path(__file__).resolve().parents[1]
ENV_VARS = {"AGENT_NAME", "DEFAULT_AGENT"}
SEAMS = {"get_active_agent", "space_agent", "default_runtime"}
#: Readers still to go, each with the package that removes it.
ALLOWED = {
    "services/cowork_agent/registry/agent_registry.py",
    "server.py",                        # P8: boot setup for every installed runtime
    "routers/auth/codex_setup.py",      # P7: the token copy is deleted
}
#: Import-time reads kept on purpose: the secret store must not move while
#: the server runs. Adapters are skipped: an exception's agent is fixed at boot.
FROZEN_OK = {"services/cowork_agent/registry/agent_env.py"}
ADAPTERS = "services/cowork_agent/adapters/"


def _sources():
    for top in ("server.py", "routers", "services", "utils"):
        path = REPO / top
        yield from ([path] if path.is_file() else sorted(path.rglob("*.py")))


def _reads_env(node: ast.AST) -> bool:
    """``os.getenv("X")``, ``os.environ.get("X")`` or ``os.environ["X"]`` for an X in ENV_VARS."""
    if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
        name = ast.unparse(node.func)
        return name in {"os.getenv", "os.environ.get", "getenv"} and node.args[0].value in ENV_VARS
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        return ast.unparse(node.value) == "os.environ" and node.slice.value in ENV_VARS
    return False


class SeamGuardTests(unittest.TestCase):
    def test_only_the_seams_read_the_agent_env(self) -> None:
        readers = sorted(str(p.relative_to(REPO)) for p in _sources()
                         if any(_reads_env(n) for n in ast.walk(ast.parse(p.read_text(encoding="utf-8")))))
        self.assertEqual(set(readers) - ALLOWED, set())

    def test_no_module_freezes_the_agent_at_import(self) -> None:
        frozen = []
        for path in _sources():
            if str(path.relative_to(REPO)) in FROZEN_OK or str(path.relative_to(REPO)).startswith(ADAPTERS):
                continue
            for stmt in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and any(
                        isinstance(n, ast.Call) and ast.unparse(n.func).split(".")[-1] in SEAMS
                        for n in ast.walk(stmt)):
                    frozen.append(f"{path.relative_to(REPO)}:{stmt.lineno}")
        self.assertEqual(frozen, [])

    def test_runtimes_are_exactly_the_adapters_with_a_storm_runtime(self) -> None:
        self.assertEqual({m.name for m in reg.runtimes()}, set(list_capability_providers("runtime")))
        ranks = [m.boot_rank for m in reg.runtimes()]
        self.assertEqual(len(set(ranks)), len(ranks))


class SeamOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ranked = [m.name for m in reg.runtimes()]
        self.exception = next(m.name for m in reg.all_agents() if m.boot_rank is None)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in ENV_VARS:
            os.environ.pop(var, None)
        self.addCleanup(reg.set_preferred_runtime, None)

    def test_the_order_is_setting_agent_name_default_agent_then_installed_by_rank(self) -> None:
        first, second, third = self.ranked[:3]
        with mock.patch.object(reg.shutil, "which", side_effect=lambda b: b == reg.get_agent(third).binary):
            self.assertEqual(reg.default_runtime(), third)          # nothing set: the first installed
            os.environ["DEFAULT_AGENT"] = second
            self.assertEqual(reg.default_runtime(), second)
            os.environ["AGENT_NAME"] = first
            self.assertEqual(reg.default_runtime(), first)
            reg.set_preferred_runtime(third)
            self.assertEqual(reg.default_runtime(), third)          # the Space setting wins

    def test_nothing_installed_falls_back_to_the_safe_boot_default(self) -> None:
        with mock.patch.object(reg.shutil, "which", return_value=None):
            self.assertEqual(reg.default_runtime(), reg._SAFE_BOOT)

    def test_an_exception_space_keeps_its_own_agent_and_modules(self) -> None:
        os.environ["AGENT_NAME"] = self.exception
        reg.set_preferred_runtime(self.ranked[0])
        self.assertEqual(reg.space_agent(), self.exception)
        self.assertEqual(reg.get_active_agent().name, self.exception)
        self.assertEqual(reg.default_runtime(), self.ranked[0])       # threads still run on a runtime
        module = try_load_capability("adapter")
        self.assertEqual(module.__name__, f"services.cowork_agent.adapters.{self.exception}.adapter")

    def test_a_runtime_space_follows_the_setting_without_a_restart(self) -> None:
        os.environ["AGENT_NAME"] = self.ranked[0]
        self.assertEqual(reg.space_agent(), self.ranked[0])
        reg.set_preferred_runtime(self.ranked[1])
        self.assertEqual(reg.space_agent(), self.ranked[1])

    def test_an_unknown_name_is_skipped_with_a_warning(self) -> None:
        os.environ["AGENT_NAME"] = "no-such-agent"
        reg.set_preferred_runtime(self.ranked[1])
        with self.assertWarns(UserWarning):
            self.assertEqual(reg.space_agent(), self.ranked[1])


if __name__ == "__main__":
    unittest.main()
