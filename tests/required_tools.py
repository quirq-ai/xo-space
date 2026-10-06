"""The external tools the repo-wide checks shell out to (bash, git, node).

A missing tool fails the check by default, so no runner (CI, the quirq infra
presubmit, a `qq test` run) can report one of these checks green by skipping
it. A contributor without node, say, can set XO_ALLOW_MISSING_TOOLS=1 to skip
those checks instead; that opt-out is ignored when CI is set.

The flags, PATH and the whole environment are read once, when the tests
package is first imported (tests/__init__.py imports this module) and before
any test imports server.py, which loads .env files with override=True. A data
file in a PR therefore cannot switch the opt-out on, turn CI off, or hide a
tool. Every subprocess a repo check or shell harness starts gets CLEAN_ENV:
that snapshot minus the variables that make bash, node, git or Python run
code or read files the checkout chooses (BASH_ENV, NODE_OPTIONS, PYTHON*,
...), so a .env cannot reach the children either.

Two tests back the snapshot up (tests/test_checkout_hygiene.py): git must
track no .env file, and under pytest server.py must not have been imported
before this module (a `-p server` in pytest.ini would do that).
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest

FALSE_VALUES = {"", "0", "false", "no", "off"}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in FALSE_VALUES


ALLOW_MISSING = _flag("XO_ALLOW_MISSING_TOOLS") and not _flag("CI")
SEARCH_PATH = os.environ.get("PATH", os.defpath)
SERVER_IMPORTED_FIRST = "server" in sys.modules

# Each makes a child shell, node, git, the dynamic loader or Python load code
# or config from a path in the variable.
_UNSAFE_NAMES = frozenset({
    "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PS4", "PROMPT_COMMAND", "CDPATH", "GLOBIGNORE",
    "NODE_OPTIONS", "NODE_PATH", "NODE_EXTRA_CA_CERTS",
})
_UNSAFE_PREFIXES = ("PYTHON", "GIT_", "LD_", "DYLD_", "BASH_FUNC_")
CLEAN_ENV = {
    key: value for key, value in os.environ.items()
    if key not in _UNSAFE_NAMES and not key.startswith(_UNSAFE_PREFIXES)
}
CLEAN_ENV["PATH"] = SEARCH_PATH


def clean_env(*, drop: tuple[str, ...] = (), **extra: str) -> dict[str, str]:
    """A copy of CLEAN_ENV for one subprocess, minus `drop`, plus `extra`."""
    env = {key: value for key, value in CLEAN_ENV.items() if key not in drop}
    env.update(extra)
    return env


def require_tools(test: unittest.TestCase, *names: str) -> dict[str, str]:
    """Each tool's path; fail (or, when allowed, skip) the test if one is missing."""
    paths = {name: shutil.which(name, path=SEARCH_PATH) for name in names}
    missing = sorted(name for name, path in paths.items() if path is None)
    if missing:
        if ALLOW_MISSING:
            test.skipTest(f"{', '.join(missing)} not installed (XO_ALLOW_MISSING_TOOLS is set)")
        test.fail(f"{', '.join(missing)} not on PATH; install it, or set XO_ALLOW_MISSING_TOOLS=1 "
                  "to skip this check on a machine without it (ignored when CI is set)")
    return paths
