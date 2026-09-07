from __future__ import annotations

import unittest

from services.cowork_agent.project_sharing.repo_identity import normalize_repo
from services.cowork_agent.project_sharing.state import state_path


class RepoIdentityTests(unittest.TestCase):
    def test_normalize_repo_vectors(self) -> None:
        # Shared vector table with xo-swarm-api utils/repo_identity.py. Keep in sync.
        cases = [
            ("https://github.com/Acme/Trip-Planner.git", "github.com/acme/trip-planner"),
            ("git@github.com:acme/trip-planner.git", "github.com/acme/trip-planner"),
            ("ssh://git@github.com/acme/trip-planner", "github.com/acme/trip-planner"),
            ("https://user:tok@github.com/acme/trip-planner/", "github.com/acme/trip-planner"),
            ("  https://gitlab.com/group/sub/repo.git  ", "gitlab.com/group/sub/repo"),
            ("github.com/acme/trip-planner", "github.com/acme/trip-planner"),
            ("https://github.com", None),
            ("https://github.com/acme", None),
            ("not a url", None),
            ("", None),
            (None, None),
            (42, None),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_repo(raw), expected)

    def test_state_filename_is_stable_and_collision_free(self) -> None:
        a = state_path("github.com/acme/trip-planner")
        b = state_path("github.com/acme/trip-planner")
        c = state_path("github.com/acme__trip-planner")
        self.assertEqual(a, b)
        self.assertNotEqual(a.name, c.name)
        self.assertTrue(a.name.startswith("github.com__acme__trip-planner-"))
        self.assertTrue(a.name.endswith(".json"))
        self.assertRegex(a.name, r"-[0-9a-f]{8}\.json$")

    def test_state_filename_never_escapes_the_relay_dir(self) -> None:
        p = state_path("evil.example/../../etc/passwd")
        self.assertEqual(p.parent.name, "project_sharing")
        self.assertNotIn("..", p.name)
        self.assertNotIn("/", p.name)
