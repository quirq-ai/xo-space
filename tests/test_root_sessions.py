"""Sessions started with no project (issue #146).

A chat with neither ``agent_id`` nor ``workspace`` runs in the projects root.
Before this, its index row was handed to ``write_session_row("default", …)``,
which refused because no ``default`` folder exists, and nothing logged the
refusal; the session then vanished from ``/api/sessions``, ``/api/messages``
and the transcript.

Such a session now has no project id at all: an empty project segment in its
key (``claude::web:<hex>``), which no folder can ever match, and an index home
at the Space level, ``~/.quirq/sessions/sessionslist.d/``, outside the
per-project ``~/.quirq/projects/<key>/`` namespace. A plain chat on storm
started with no folder lands there too (``tests/test_storm_plain_chat.py``).
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.engine import sessions_io
from services.cowork_agent.visualizer.workspace import sessionslist as workspace_sessionslist
from services.storage import layout

SID = "4b9ce62a-0000-4000-8000-000000000146"
ROOT_KEY = "claude::web:abcd1234"


def _row(session_id: str = SID, backend: str = "claude_code", directory: str = "") -> dict:
    return {
        "sessionId": session_id,
        "nativeSessionId": "native-" + session_id,
        "directory": directory,
        "backend": backend,
        "updatedAt": 1_700_000_000_000,
        "usage": {},
    }


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.state = base / "state"
        self.projects = base / "projects"
        self.state.mkdir()
        self.projects.mkdir()
        env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.state),
            "XO_PROJECTS_ROOT": str(self.projects),
        })
        env.start()
        self.addCleanup(env.stop)
        workspace_sessionslist.reset_caches()

    def make_project(self, name: str) -> Path:
        pdir = self.projects / name
        (pdir / ".xo").mkdir(parents=True)
        (pdir / ".xo" / "project.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        return pdir

    def projects_root_entries(self) -> list[str]:
        return sorted(p.name for p in self.projects.iterdir())


class IndexWriteTests(_Sandbox):
    def test_root_row_is_written_at_the_space_level_with_no_project_folder(self) -> None:
        self.assertTrue(sessions_io.write_session_row("", ROOT_KEY, _row(directory=str(self.projects))))
        shard_dir = self.state / "sessions" / "sessionslist.d"
        self.assertEqual(shard_dir, layout.sessions_dir() / "sessionslist.d")
        self.assertTrue((shard_dir / sessions_io.shard_filename(ROOT_KEY)).exists())
        # Outside the per-project namespace, and nothing under the projects root.
        self.assertFalse((self.state / "projects").exists())
        self.assertEqual(self.projects_root_entries(), [])
        self.assertEqual(sessions_io.read_root_session_index()[ROOT_KEY]["sessionId"], SID)
        self.assertEqual(sessions_io.read_session_index("")[ROOT_KEY]["sessionId"], SID)

    def test_no_project_name_can_reach_the_root_index(self) -> None:
        """The root's identity is the absence of a name, so a project called
        anything at all reads and writes its own home, never the root's."""
        sessions_io.write_session_row("", ROOT_KEY, _row("sid-root"))
        for name in ("root", "default", "sessions"):
            with self.subTest(project=name):
                self.make_project(name)
                self.assertEqual(sessions_io.read_session_index(name), {})
                self.assertTrue(sessions_io.write_session_row(name, f"claude:{name}:web:1", _row("sid-" + name)))
        self.assertEqual(list(sessions_io.read_root_session_index()), [ROOT_KEY])

    def test_a_refused_index_write_is_logged_with_the_session_key(self) -> None:
        key = "claude:no-such-project:web:abcd1234"
        with self.assertLogs(sessions_io.__name__, level="WARNING") as captured:
            self.assertFalse(sessions_io.write_session_row("no-such-project", key, _row()))
        self.assertTrue(any(key in line and "no-such-project" in line for line in captured.output), captured.output)


class ReaderTests(_Sandbox):
    def test_root_index_is_iterated_first_with_the_projects_root_as_its_directory(self) -> None:
        self.make_project("blackhole")
        sessions_io.write_session_row("blackhole", "claude:blackhole:web:1", _row("sid-project"))
        sessions_io.write_session_row("", ROOT_KEY, _row())
        seen = list(sessions_io.iter_project_session_indexes())
        self.assertEqual(
            [(pid, pdir) for pid, pdir, _ in seen],
            [("", self.projects), ("blackhole", self.projects / "blackhole")],
        )
        self.assertIn(ROOT_KEY, seen[0][2])

    def test_root_session_is_listed_and_owned(self) -> None:
        sessions_io.write_session_row("", ROOT_KEY, _row(directory=str(self.projects)))
        listed = [s for s in sessions_io.load_all_sessions() if s["id"] == SID]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["agent"], "")
        self.assertEqual(listed[0]["directory"], str(self.projects))
        self.assertEqual(sessions_io.find_session_backend(SID), "claude_code")

    def test_root_and_project_sessions_list_together(self) -> None:
        self.make_project("blackhole")
        sessions_io.write_session_row("blackhole", "claude:blackhole:web:1", _row("sid-project"))
        sessions_io.write_session_row("", "claude::web:2", _row("sid-root"))
        agents = {s["id"]: s["agent"] for s in sessions_io.load_all_sessions() if s["id"].startswith("sid-")}
        self.assertEqual(agents, {"sid-project": "blackhole", "sid-root": ""})

    def test_workspace_union_includes_root_sessions(self) -> None:
        sessions_io.write_session_row("", ROOT_KEY, _row())
        self.assertTrue(workspace_sessionslist.apply())
        doc = json.loads((project_layout.workspace_sessions_dir() / "sessionslist.json").read_text(encoding="utf-8"))
        self.assertEqual(doc[ROOT_KEY]["sessionId"], SID)


if __name__ == "__main__":
    unittest.main()
