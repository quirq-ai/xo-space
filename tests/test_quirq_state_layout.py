"""The machine-local state root has one folder per subject.

``tests/fixtures/quirq-state/`` is the sample: the folders ``~/.quirq/`` holds,
with a README saying what goes in each. These tests hold the code to it:

1. ``services/storage/layout.py`` names exactly the sample's folders;
2. every store writes inside one of them;
3. an install from before the state root had folders ends up with nothing
   outside them once the boot migration has run.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import usage_sync
from services.connections import store as connections_store
from services.cowork_agent import project_layout, runtime_config, xo_cowork_state
from services.cowork_agent.connectors import token_store
from services.cowork_agent.project_sharing import state as sharing_state
from services.cowork_agent.visualizer import state as watcher_state
from services.inbox import store as inbox_store
from services.storage import flock, layout
from utils import commands
from utils.commands import scheduler

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "quirq-state"
PID = "7deb4a22-0789-497d-9399-a2272579fa06"


def _sample_folders() -> list[str]:
    return sorted(p.name for p in FIXTURE.iterdir() if p.is_dir())


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root = base / "state"
        self.root.mkdir()
        env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.root),
            "XO_PROJECTS_ROOT": str(base / "projects"),
            "QUIRQ_RUNTIME_FILE": "",
            "QUIRQ_SECRETS_FILE": "",
            "QUIRQ_COMMAND_LOG": "",
            "QUIRQ_COMMAND_LOG_PATH": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def folder_of(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root.resolve()).parts[0]


class SampleTests(_Sandbox):
    def test_layout_names_exactly_the_sample_folders(self) -> None:
        named = {
            layout.projects_dir(), layout.inbox_dir(), layout.sharing_dir(),
            layout.usage_dir(), layout.settings_dir(), layout.secrets_dir(),
            layout.cache_dir(), layout.logs_dir(), layout.locks_dir(),
            self.root / "connections", self.root / "scheduler",
        }
        self.assertEqual(sorted(p.name for p in named), _sample_folders())

    def test_the_readme_describes_every_folder(self) -> None:
        readme = (FIXTURE / "README.md").read_text(encoding="utf-8")
        for name in _sample_folders():
            self.assertIn(f"`{name}/`", readme)


class StorePathTests(_Sandbox):
    def test_every_store_writes_inside_a_sample_folder(self) -> None:
        paths = {
            "project history": project_layout.runtime_dir(PID),
            "the Space timeline": project_layout.workspace_timeline_path(),
            "watcher reading positions": watcher_state.watcher_state_dir(),
            "the Inbox": inbox_store.inbox_path(),
            "a connection": connections_store.connection_dir("gmail"),
            "saved commands": scheduler.scheduler_dir(),
            "a command's output": scheduler.log_file("job1"),
            "the command log": commands._default_command_log_path(),
            "sharing bookmarks": sharing_state.relay_state_dir(),
            "runtime settings": runtime_config.runtime_config_file(),
            "saved roots": runtime_config.root_config_file(),
            "rebuilt views": project_layout.workspace_runtime_dir(),
            "the heartbeat": watcher_state.watcher_heartbeat_path(),
            "live presence": watcher_state.project_activity_path("demo"),
            "a lock": flock._lock_path_for(Path("/elsewhere/.xo/todos.json")),
        }
        folders = set(_sample_folders())
        for what, path in paths.items():
            with self.subTest(store=what):
                self.assertIn(self.folder_of(path), folders, path)

    def test_stores_resolved_at_import_use_their_folders(self) -> None:
        # Resolved once at import against the real state root, so only the
        # folder they sit in can be compared.
        self.assertEqual(xo_cowork_state.STATE_FILE.parent.name, "settings")
        self.assertEqual(Path(usage_sync._DEFAULT_WATERMARK_PATH).parent.name, "usage")
        self.assertEqual(token_store._DEFAULT_TOKEN_FILE.parent.name, "secrets")
        self.assertIn(Path.home() / ".config" / "token.json", token_store._LEGACY_TOKEN_FILES)


class MigrationTests(_Sandbox):
    def test_an_install_from_before_the_folders_ends_up_inside_them(self) -> None:
        files = (
            "inbox.json", "roots.env", "runtime.env", "state.json", "secrets.env",
            "commands.log", "commands.log.1",
            "project_sharing/github.com__acme__app-1234abcd.json",
            "watcher/offsets.json", "watcher/sample-offsets.json", "watcher/heartbeat.json",
            "watcher/activity/workspace.json", "watcher/activity/projects/demo.json",
            "watcher/locks/todos.json.abcd1234.lock",
            "workspace/timeline.jsonl", "workspace/graph.json", "workspace/dashboard.json",
            "workspace/sessions.json", "workspace/stats.json",
            "workspace/sessions/sessionslist.json",
            "scheduler/jobs.json", "scheduler/logs/job1.log",
            f"projects/{PID}/stats.json",
            "connections/gmail/config.json",
        )
        for name in files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")

        layout.migrate_layout()

        left = {p.name for p in self.root.iterdir()}
        self.assertLessEqual(left, set(_sample_folders()), sorted(left - set(_sample_folders())))
        self.assertEqual(layout.migrate_layout(), [])


if __name__ == "__main__":
    unittest.main()
