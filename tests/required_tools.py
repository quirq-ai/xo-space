"""The external tools the repo-wide checks shell out to (bash, git, node).

A missing tool fails the check by default, so no runner (CI, the quirq infra
presubmit, a `qq test` run) can report one of these checks green by skipping
it. A contributor without node, say, can set XO_ALLOW_MISSING_TOOLS=1 to skip
those checks instead; that opt-out is ignored when CI is set.
"""

from __future__ import annotations

import os
import shutil
import unittest

FALSE_VALUES = {"", "0", "false", "no", "off"}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in FALSE_VALUES


def require_tools(test: unittest.TestCase, *names: str) -> dict[str, str]:
    """Each tool's path; fail (or, when allowed, skip) the test if one is missing."""
    paths = {name: shutil.which(name) for name in names}
    missing = sorted(name for name, path in paths.items() if path is None)
    if missing:
        if _flag("XO_ALLOW_MISSING_TOOLS") and not _flag("CI"):
            test.skipTest(f"{', '.join(missing)} not installed (XO_ALLOW_MISSING_TOOLS is set)")
        test.fail(f"{', '.join(missing)} not on PATH; install it, or set XO_ALLOW_MISSING_TOOLS=1 "
                  "to skip this check on a machine without it (ignored when CI is set)")
    return paths
