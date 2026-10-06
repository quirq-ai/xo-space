"""What the checkout itself may carry, so data files cannot steer the suite.

server.py loads the checkout's ``.env`` and then a runtime file with
override=True, so one force-added ``.env`` could set the variables that make
bash, node or Python run code of its choosing, re-point AGENT_NAME, or turn CI
off. tests/required_tools.py snapshots the environment before that happens;
these tests keep the two ways around the snapshot shut:

- git tracks no ``.env`` file (the gitignore can be bypassed with ``add -f``);
- under pytest, server.py is not imported before the tests package (as an
  ``addopts = -p server`` in pytest.ini would do), which would load the
  ``.env`` files before the snapshot is taken.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path, PurePosixPath

from tests.required_tools import SERVER_IMPORTED_FIRST, clean_env, require_tools

ROOT = Path(__file__).resolve().parents[1]

# Tracked on purpose: the template, and the fixture state root that tests
# point QUIRQ_STATE_ROOT at explicitly.
ALLOWED_ENV_FILES = frozenset({
    ".env.example",
    "tests/fixtures/quirq-state/secrets/secrets.env",
    "tests/fixtures/quirq-state/settings/roots.env",
    "tests/fixtures/quirq-state/settings/runtime.env",
})


def is_env_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name == ".env" or name.endswith(".env") or name.startswith(".env.")


class CheckoutHygiene(unittest.TestCase):
    def test_no_tracked_env_files(self) -> None:
        git = require_tools(self, "git")["git"]
        proc = subprocess.run([git, "ls-files", "-z"], cwd=ROOT, env=clean_env(),
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            self.fail(f"git ls-files exited {proc.returncode}: {proc.stderr.strip()}")
        tracked = [path for path in proc.stdout.split("\0") if path]
        self.assertTrue(tracked, "git ls-files listed nothing")
        unexpected = sorted(p for p in tracked if is_env_file(p) and p not in ALLOWED_ENV_FILES)
        self.assertEqual(unexpected, [], "env files must not be committed: server.py and "
                         "load_dotenv() load them into the test process")

    @unittest.skipUnless("_pytest" in sys.modules, "load order is only fixed under pytest")
    def test_server_not_imported_before_tests_package(self) -> None:
        self.assertFalse(SERVER_IMPORTED_FIRST, "server.py was imported before the tests package "
                         "(a `-p server` plugin or a conftest.py?), so it loaded the .env files "
                         "before tests/required_tools.py took its snapshot")

    def test_env_file_patterns(self) -> None:
        for path in (".env", "services/cowork_agent/registry/.env", "atk/settings/runtime.env",
                     ".env.local"):
            self.assertTrue(is_env_file(path), path)
        for path in ("environment.py", "tests/fixtures/env/README.md", ".envrc"):
            self.assertFalse(is_env_file(path), path)


if __name__ == "__main__":
    unittest.main()
