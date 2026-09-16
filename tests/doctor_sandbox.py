"""A healthy Space built from the two golden samples, for xo-doctor tests.

The state root is a copy of tests/fixtures/quirq-state/ and the projects root
holds sample-project, whose .xo/ is tests/fixtures/xo-project/.xo/; both use
one pid. The watcher is off (the sample heartbeat is from January), and every
run is judged a week from now, so no copied file counts as a recent write.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from services.doctor import run

ROOT = Path(__file__).resolve().parents[1]
STATE_FIXTURE = ROOT / "tests" / "fixtures" / "quirq-state"
PROJECT_FIXTURE = ROOT / "tests" / "fixtures" / "xo-project" / ".xo"
PID = "00000000-0000-4000-8000-000000000000"
WEEK = 7 * 86400


def snapshot(*roots: Path) -> dict[str, tuple[int, int, int]]:
    """``(inode, size, mtime_ns)`` for every entry, directories included."""
    seen: dict[str, tuple[int, int, int]] = {}
    for root in roots:
        if not os.path.lexists(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            for path in (Path(dirpath), *(Path(dirpath, n) for n in (*dirnames, *filenames))):
                info = path.lstat()
                seen[str(path)] = (info.st_ino, info.st_size, info.st_mtime_ns)
    return seen


class DoctorSandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.state = base / "state"
        self.projects = base / "projects"
        shutil.copytree(STATE_FIXTURE, self.state, ignore=shutil.ignore_patterns("README.md"))
        shutil.copytree(PROJECT_FIXTURE, self.projects / "sample-project" / ".xo")
        env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.state),
            "XO_PROJECTS_ROOT": str(self.projects),
            "QUIRQ_WATCHER_ENABLED": "false",
            "XO_SPACE_ID": "",
            "QUIRQ_HOST_STATE_ROOT": "",
            "QUIRQ_HOST_PROJECTS_ROOT": "",
            "QUIRQ_COMMAND_LOG_PATH": "",
            "QUIRQ_RUNTIME_FILE": "",
            "QUIRQ_SECRETS_FILE": "",
        })
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time() + WEEK

    def report(self, now: float | None = None) -> dict:
        return run.run_checks(now=self.now if now is None else now)

    def problems(self, report: dict | None = None) -> list[dict]:
        report = report or self.report()
        return [f for c in report["checks"] for f in c["findings"] if f["level"] != "OK"]

    def ids(self, report: dict | None = None) -> set[str]:
        return {f["id"] for f in self.problems(report)}
