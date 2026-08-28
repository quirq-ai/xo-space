from __future__ import annotations

import importlib
import shutil
import unittest
from pathlib import Path

import services.cowork_agent.adapters as adapters_pkg
from services.cowork_agent.adapters.loader import try_load_capability

# A throwaway adapter package created inside the real adapters directory —
# the loader resolves capabilities by import path, so there is no other seam
# to point it at. Underscore-prefixed and removed again in addCleanup; no
# config/agents manifest exists for it, so discovery never sees it as a real
# runtime.
_AGENT = "_loader_contract_test"


class TryLoadCapabilityContractTests(unittest.TestCase):
    """Pin the fail-loud contract of try_load_capability.

    A capability file that simply is not there is "unsupported" and degrades
    to None. A capability file that exists but cannot import (one of its OWN
    dependencies is missing) is an implementation error and must re-raise —
    silently dropping its routes would disguise a broken install as an
    unsupported feature.
    """

    def setUp(self) -> None:
        adapters_dir = Path(adapters_pkg.__file__).resolve().parent
        self.agent_dir = adapters_dir / _AGENT
        self.agent_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.agent_dir, True)
        self.addCleanup(importlib.invalidate_caches)
        (self.agent_dir / "__init__.py").write_text("")
        (self.agent_dir / "working.py").write_text("VALUE = 42\n")
        (self.agent_dir / "broken.py").write_text(
            "import a_module_that_does_not_exist_anywhere\n"
        )
        importlib.invalidate_caches()

    def test_present_capability_loads(self) -> None:
        module = try_load_capability("working", agent=_AGENT)
        self.assertIsNotNone(module)
        self.assertEqual(module.VALUE, 42)

    def test_missing_capability_degrades_to_none(self) -> None:
        self.assertIsNone(try_load_capability("absent", agent=_AGENT))

    def test_missing_agent_package_degrades_to_none(self) -> None:
        self.assertIsNone(try_load_capability("working", agent="_no_such_agent"))

    def test_broken_capability_fails_loud(self) -> None:
        with self.assertRaises(ModuleNotFoundError) as ctx:
            try_load_capability("broken", agent=_AGENT)
        # The error names the actual missing dependency, not the capability.
        self.assertEqual(
            ctx.exception.name, "a_module_that_does_not_exist_anywhere"
        )


if __name__ == "__main__":
    unittest.main()
