"""One breakage on top of the healthy samples gives the expected finding."""

from __future__ import annotations

import json
import os
import shutil
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from services.timestamps import iso
from tests.doctor_sandbox import DoctorSandbox


class SpaceIdentityTests(DoctorSandbox):
    def write_space(self, xo_space_id, *, age_s: float = 3600, roots: dict | None = None) -> None:
        xo = self.projects / ".xo"
        xo.mkdir(exist_ok=True)
        updated = iso(datetime.fromtimestamp(self.now - age_s, timezone.utc))
        document = {"$schema": "xo/space.schema.json", "schema": 2, "xo_space_id": xo_space_id,
                    "updated_at": updated,
                    "roots": roots or {"projects_root": str(self.projects), "state_root": str(self.state)}}
        (xo / "space.json").write_text(json.dumps(document), encoding="utf-8")

    def identity(self) -> list[dict]:
        return [f for f in self.problems() if f["id"] == "space.identity"]

    def test_null_id_while_the_environment_has_one_fails(self) -> None:
        self.write_space(None)
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-1"}):
            [finding] = self.identity()
        self.assertEqual(finding["level"], "FAIL")

    def test_a_different_id_fails(self) -> None:
        self.write_space("space-2")
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-1"}):
            [finding] = self.identity()
        self.assertEqual(finding["level"], "FAIL")
        self.assertIn("space-2", finding["observed"])

    def test_an_id_with_no_environment_value_warns(self) -> None:
        self.write_space("space-1")
        [finding] = self.identity()
        self.assertEqual(finding["level"], "WARN")

    def test_matching_id_is_healthy(self) -> None:
        self.write_space("space-1")
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-1"}):
            self.assertEqual(self.problems(), [])

    def test_stale_roots_warn_but_not_right_after_a_write(self) -> None:
        other = {"projects_root": "/elsewhere/projects", "state_root": str(self.state)}
        self.write_space("space-1", roots=other, age_s=3600)
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-1"}):
            self.assertEqual([f["subject"] for f in self.identity()], ["space.json roots"])
            self.write_space("space-1", roots=other, age_s=10)
            self.assertEqual(self.identity(), [])

    def test_an_unreadable_space_json_is_left_to_the_read_check(self) -> None:
        (self.projects / ".xo").mkdir()
        (self.projects / ".xo" / "space.json").write_text("{", encoding="utf-8")
        with patch.dict(os.environ, {"XO_SPACE_ID": "space-1"}):
            self.assertEqual(self.ids(), {"read.invalid_json"})


class DuplicateIdTests(DoctorSandbox):
    def test_two_folders_with_one_pid_warn(self) -> None:
        shutil.copytree(self.projects / "sample-project", self.projects / "copy")
        [finding] = [f for f in self.problems() if f["id"] == "projects.duplicate_id"]
        self.assertEqual(finding["level"], "WARN")
        self.assertIn("copy", finding["observed"])
        self.assertIn("sample-project", finding["observed"])


if __name__ == "__main__":
    unittest.main()
