"""A healthy Space built from the two golden samples, for xo-doctor tests.

The state root is a copy of tests/fixtures/quirq-state/ and the projects root
holds sample-project, whose .xo/ is tests/fixtures/xo-project/.xo/; both use
one pid. The watcher and the background pollers are off, and there is
no task record (the doctor runs as if outside the server), and every
run is judged a week from now, so no copied file counts as a recent write.
"""

from __future__ import annotations

import collections
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

_Statvfs = collections.namedtuple("_Statvfs", "f_frsize f_bavail f_files f_favail")
#: 20 GB and 900,000 inodes free: every sandbox run sees a roomy disk unless
#: a test patches os.statvfs itself.
ROOMY_DISK = _Statvfs(f_frsize=4096, f_bavail=20 * 1024**3 // 4096, f_files=1_000_000, f_favail=900_000)


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
        # git cannot carry 0600, so the sample's private files arrive 0644.
        # A real install writes them 0600; make the baseline match.
        for pattern in ("secrets/*", "settings/*.env"):
            for path in self.state.glob(pattern):
                path.chmod(0o600)
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
            # The golden samples carry January timestamps (poll records,
            # mirrors, schedules); each liveness test turns its source on.
            "XO_CONNECTIONS_POLL_ENABLED": "false",
            "XO_GITHUB_POLL_ENABLED": "false",
            "XO_SCHEDULER_ENABLED": "false",
        })
        env.start()
        self.addCleanup(env.stop)
        self.now = time.time() + WEEK
        disk = patch.object(os, "statvfs", return_value=ROOMY_DISK)
        disk.start()
        self.addCleanup(disk.stop)
        # The sandbox is "outside the server": no background task record, so
        # the in-process liveness layer stays silent unless a test sets one.
        components = patch("services.doctor.context._components_snapshot", return_value={})
        components.start()
        self.addCleanup(components.stop)

    def report(self, now: float | None = None) -> dict:
        return run.run_checks(now=self.now if now is None else now)

    def problems(self, report: dict | None = None) -> list[dict]:
        report = report or self.report()
        return [f for c in report["checks"] for f in c["findings"] if f["level"] != "OK"]

    def ids(self, report: dict | None = None) -> set[str]:
        return {f["id"] for f in self.problems(report)}
