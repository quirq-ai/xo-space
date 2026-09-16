"""The machine-local state root has one folder per subject.

``tests/fixtures/quirq-state/`` is the sample: the folders ``~/.quirq/`` holds,
with a README saying what goes in each. These tests hold the code to it:

1. ``services/storage/layout.py`` names exactly the sample's folders;
2. every store writes inside one of them;
3. an install from before the state root had folders ends up with nothing
   outside them once the boot migration has run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import telemetry_sources, usage_sync
from services.connections import store as connections_store
from services.cowork_agent import project_layout, runtime_config, xo_cowork_state
from routers.cowork_agent.bff._visualizer_models import TimelineEvent
from services.cowork_agent.connectors import token_store
from services.cowork_agent.engine import sessions_io
from services.cowork_agent.registry import agent_env
from services.cowork_agent.visualizer import workitem_claims
from services.cowork_agent.visualizer.ingest.jsonl_tail import OffsetStore
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
            layout.connections_dir(), layout.scheduler_dir(),
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
            "telemetry settings": telemetry_sources.settings_path(),
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


# ── The example files ─────────────────────────────────────────────────────────

EXAMPLE_PROJECT = "sample-project"
EXAMPLE_PID = "00000000-0000-4000-8000-000000000000"
EXAMPLE_SESSION = "11111111-1111-4111-8111-111111111111"
EXAMPLE_WORKITEM = "22222222-2222-4222-8222-222222222222"
SCHEMAS = ROOT / "services" / "cowork_agent" / "visualizer" / "schema"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
#: Keyed by name, so a ``schema`` key would read as an entry.
_KEYED_BY_NAME = ("secrets/token.json", "cache/sessions/sessionslist.json")


def _examples(suffix: str) -> list[Path]:
    return sorted(p for p in FIXTURE.rglob(f"*{suffix}") if p.is_file())


def _rel(path: Path) -> str:
    return path.relative_to(FIXTURE).as_posix()


def _documents(path: Path) -> list:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return [json.loads(text)]


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


class ExampleRuleTests(unittest.TestCase):
    """Every folder has an example, and the examples follow the four rules."""

    def test_every_folder_has_an_example(self) -> None:
        for name in _sample_folders():
            with self.subTest(folder=name):
                self.assertTrue(any(p.is_file() for p in (FIXTURE / name).rglob("*")), f"{name}/ has no example")

    def test_rule_1_project_records_carry_the_pid(self) -> None:
        for path in (FIXTURE / "projects").rglob("timeline.jsonl"):
            for line in _documents(path):
                with self.subTest(file=_rel(path), type=line["type"]):
                    self.assertEqual(line["pid"], EXAMPLE_PID)
                    if path.parent.name == "projects":
                        self.assertEqual(line["project_id"], EXAMPLE_PROJECT)
        for item in _documents(FIXTURE / "inbox" / "inbox.json")[0]["items"]:
            if item.get("project_id"):
                self.assertEqual(item["pid"], EXAMPLE_PID)

    def test_rule_2_times_end_in_z(self) -> None:
        for path in _examples(".json") + _examples(".jsonl"):
            for text in (s for document in _documents(path) for s in _strings(document)):
                if _TIMESTAMP.match(text):
                    with self.subTest(file=_rel(path), value=text):
                        self.assertTrue(text.endswith("Z"))

    def test_rule_3_event_lines_start_with_ts_and_type(self) -> None:
        for path in _examples(".jsonl"):
            for line in _documents(path):
                with self.subTest(file=_rel(path)):
                    self.assertEqual(list(line)[:2], ["ts", "type"])

    def test_rule_4_data_files_carry_a_schema_number(self) -> None:
        for path in _examples(".json"):
            rel = _rel(path)
            if rel in _KEYED_BY_NAME or "/sessionslist.d/" in rel:
                continue
            with self.subTest(file=rel):
                self.assertIsInstance(_documents(path)[0].get("schema"), int)


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
class ExampleSchemaTests(unittest.TestCase):
    def validate(self, schema_name: str, document) -> None:
        import jsonschema

        schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
        jsonschema.Draft7Validator(schema).validate(document)

    def test_documents_match_their_schemas(self) -> None:
        pairs = {
            f"projects/{EXAMPLE_PID}/stats.json": "stats.schema.json",
            f"projects/{EXAMPLE_PID}/sessions/sessions-augment.json": "sessions-augment.schema.json",
            f"projects/{EXAMPLE_PID}/github/issues.json": "github-issues.schema.json",
            "cache/stats.json": "stats.schema.json",
            "cache/sessions/sessions-augment.json": "sessions-augment.schema.json",
            "cache/activity/workspace.json": "activity.schema.json",
            f"cache/activity/projects/{EXAMPLE_PROJECT}.json": "activity.schema.json",
            "inbox/inbox.json": "inbox.schema.json",
        }
        for rel, schema_name in pairs.items():
            with self.subTest(file=rel):
                self.validate(schema_name, _documents(FIXTURE / rel)[0])

    def test_timeline_lines_match_the_schema_and_the_route_model(self) -> None:
        for path in (FIXTURE / "projects").rglob("timeline.jsonl"):
            for line in _documents(path):
                with self.subTest(file=_rel(path), type=line["type"]):
                    self.validate("timeline.schema.json", line)
                    TimelineEvent(**line)


class ExampleStoreTests(unittest.TestCase):
    """The stores that own each example read it back, from a copy of the sample."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root = base / "state"
        shutil.copytree(FIXTURE, self.root, ignore=shutil.ignore_patterns("README.md"))
        env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.root), "XO_PROJECTS_ROOT": str(base / "projects"),
            "QUIRQ_RUNTIME_FILE": "", "QUIRQ_SECRETS_FILE": "", "QUIRQ_COMMAND_LOG": "off",
            "QUIRQ_COMMAND_LOG_PATH": "", "XO_SCHEDULER_ENABLED": "false",
        })
        env.start()
        self.addCleanup(env.stop)

    def test_the_sample_needs_no_migration(self) -> None:
        self.assertEqual(layout.migrate_layout(), [])

    def test_the_inbox(self) -> None:
        document, ok = inbox_store.load_document()
        self.assertTrue(ok)
        self.assertEqual(len(document["items"]), 2)

    def test_a_connection(self) -> None:
        self.assertEqual(connections_store.read_config("gmail")["collectors"], ["unread"])
        self.assertEqual(connections_store.read_state("gmail")["events_total"], 1)
        [event] = connections_store.read_events("gmail")
        self.assertEqual(event["type"], "unread")
        self.assertEqual(connections_store.read_accounts()["gmail"]["label"], "you@example.com")

    def test_the_scheduler(self) -> None:
        [job] = scheduler.list_jobs()
        [run] = scheduler.list_runs(job["id"])
        self.assertEqual((run["type"], run["status"]), ("job.run", "ok"))

    def test_sharing(self) -> None:
        self.assertEqual(sharing_state.load_cursor("github.com/acme/sample-project"), 42)
        self.assertTrue(sharing_state.is_removed("github.com/acme/old-experiment", Path("/home/you/xo-projects")))

    def test_project_history(self) -> None:
        runtime = self.root / "projects" / EXAMPLE_PID
        self.assertIn(EXAMPLE_WORKITEM, workitem_claims.read_claims(workitem_claims.claims_path_for(runtime)))
        index = sessions_io.read_session_index_at(runtime)
        self.assertEqual([row["nativeSessionId"] for row in index.values()], [EXAMPLE_SESSION])
        offsets = self.root / "projects" / "offsets.json"
        [(source, cursor)] = _documents(offsets)[0]["offsets"].items()
        self.assertEqual(OffsetStore(offsets, legacy_store_path=None).get(Path(source)), (cursor["offset"], cursor["inode"]))

    def test_settings(self) -> None:
        roots = runtime_config._parse_env_file(runtime_config.root_config_file(), runtime_config.ROOT_CONFIG_KEYS)
        self.assertEqual(roots["XO_PROJECTS_ROOT"], "/home/you/xo-projects")
        self.assertEqual(runtime_config._parse_env_file(runtime_config.runtime_config_file())["QUIRQ_WATCHER_ENABLED"], "true")
        settings = self.root / "settings"
        with patch.object(xo_cowork_state, "STATE_DIR", settings), \
             patch.object(xo_cowork_state, "STATE_FILE", settings / "onboarding.json"):
            self.assertTrue(xo_cowork_state.get_state()["onboarding_completed"])
        self.assertEqual(telemetry_sources.disabled_source_ids(), {"cursor"})

    def test_credentials_and_usage(self) -> None:
        with patch.object(token_store, "TOKEN_FILE", self.root / "secrets" / "token.json"), \
             patch.object(token_store, "_LEGACY_TOKEN_FILES", ()):
            self.assertEqual(token_store.get_entry("github")["auth_method"], "pat")
        entries = agent_env.parse_env_file((self.root / "secrets" / "secrets.env").read_text(encoding="utf-8"))
        self.assertEqual([entry["key"] for entry in entries], ["EXAMPLE_API_KEY"])
        with patch.object(usage_sync, "SYNC_STATE_FILE", str(self.root / "usage" / "sample_agent.json")):
            self.assertEqual(usage_sync._load_sync_state()["last_synced_date"], "2025-12-31")


if __name__ == "__main__":
    unittest.main()
