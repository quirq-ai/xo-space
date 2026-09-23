"""The private names the doctor borrows from other modules still exist.

Copying a safety rule is worse than importing a private name, so the doctor
imports three. A rename must fail here, loudly and in one place, rather than
at server start (routers/cowork_agent/__init__.py imports the doctor router).
"""

from __future__ import annotations

import importlib
import unittest


class PrivateSeamsTests(unittest.TestCase):
    SEAMS = (
        ("services.cowork_agent.project_layout", "_is_safe_runtime_key"),
        ("services.cowork_agent.quirq_catalog", "_stale_after_seconds"),
        ("services.cowork_agent.visualizer.migrate", "_pending_sources"),
        ("services.cowork_agent.runtime_config", "_as_bool"),
    )

    def test_every_borrowed_private_name_exists_and_is_callable(self) -> None:
        for module_name, attribute in self.SEAMS:
            with self.subTest(seam=f"{module_name}.{attribute}"):
                module = importlib.import_module(module_name)
                self.assertTrue(callable(getattr(module, attribute, None)))

    def test_a_renamed_seam_does_not_stop_the_doctor_importing(self) -> None:
        import services.doctor.checks as checks
        import services.doctor.leftovers as leftovers
        import services.doctor.projects as projects

        for module in (checks, leftovers, projects):
            source = open(module.__file__, encoding="utf-8").read()
            for _, attribute in self.SEAMS:
                with self.subTest(module=module.__name__, attribute=attribute):
                    self.assertNotIn(f"import {attribute}", source.split("def ", 1)[0],
                                     "borrow a private name inside the function, not at import time")


if __name__ == "__main__":
    unittest.main()
