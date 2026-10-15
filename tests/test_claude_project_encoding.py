"""Claude Code's transcript-folder encoding, and observation finding sessions
whose working directory holds ``.`` or ``_`` (a thread worktree's always does).

Claude names ``~/.claude/projects/<folder>`` by replacing every character
outside ``[A-Za-z0-9]`` of the cwd with ``-``. Encoding only ``/`` pointed
observation at folders that never exist for such paths.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.claude_code import _project_encoding, sessions, visualizer_source
from services.cowork_agent.adapters.claude_code._project_encoding import encode_path
from services.cowork_agent.visualizer import project_index
from services.cowork_agent.visualizer.ingest.jsonl_tail import OffsetStore

NATIVE = "0b5d2c1e-7f3a-4c55-9a1e-3d2f1e0c9b8a"


class EncodePathTests(unittest.TestCase):
    def test_slashes_only_path_is_unchanged_from_before(self) -> None:
        self.assertEqual(encode_path("/home/coder/xo-projects/blackhole"), "-home-coder-xo-projects-blackhole")

    def test_every_non_alphanumeric_character_becomes_a_dash(self) -> None:
        self.assertEqual(encode_path("/a/.b_c d@e"), "-a--b-c-d-e")

    def test_thread_worktree_path(self) -> None:
        self.assertEqual(
            encode_path("/home/coder/xo-projects/billing_api/.xo/worktrees/thread-cmsg_1a2b"),
            "-home-coder-xo-projects-billing-api--xo-worktrees-thread-cmsg-1a2b",
        )


class _Tree(unittest.TestCase):
    """A temp home with ``.claude/projects`` and a projects root."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.claude_projects = self.home / ".claude" / "projects"
        self.root = self.home / "xo-projects"
        self.root.mkdir(parents=True)

    def transcript(self, cwd: Path) -> Path:
        folder = self.claude_projects / encode_path(str(cwd))
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{NATIVE}.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        return path


class NativeFileTests(_Tree):
    def test_worktree_session_transcript_is_found(self) -> None:
        worktree = self.root / "billing_api" / ".xo" / "worktrees" / "thread-cmsg_1a2b"
        expected = self.transcript(worktree)
        with patch.dict(os.environ, {"HOME": str(self.home)}):
            found = sessions.resolve_native_file(
                {"nativeSessionId": NATIVE, "directory": str(worktree)}, "any")
        self.assertEqual(found, expected)

    def test_missing_transcript_is_none(self) -> None:
        with patch.dict(os.environ, {"HOME": str(self.home)}):
            found = sessions.resolve_native_file(
                {"nativeSessionId": NATIVE, "directory": str(self.root / "nothing_here")}, "any")
        self.assertIsNone(found)


class DiscoveryTests(_Tree):
    def discover(self, rows: list, project_ids: list[str]) -> list[tuple[str, Path]]:
        source = visualizer_source.Source(offsets=OffsetStore(self.home / "offsets.json"))
        with (
            patch.object(visualizer_source, "_CLAUDE_PROJECTS_DIR", self.claude_projects),
            patch.object(visualizer_source, "xo_projects_root", return_value=self.root),
            patch.object(visualizer_source, "list_project_ids", return_value=project_ids),
            patch.object(visualizer_source, "iter_sessionslist_rows", return_value=rows),
            patch.dict(os.environ, {"QUIRQ_HOST_PROJECTS_ROOT": ""}),
        ):
            return list(source._discover_jsonls())

    def test_session_row_in_a_worktree_maps_to_its_project(self) -> None:
        worktree = self.root / "billing_api" / ".xo" / "worktrees" / "thread-cmsg_1a2b"
        jsonl = self.transcript(worktree)
        row = {"nativeSessionId": NATIVE, "directory": str(worktree)}
        self.assertEqual(self.discover([("billing_api", "key", row)], []), [("billing_api", jsonl)])

    def test_session_launched_in_a_project_with_underscore_or_dot_is_discovered(self) -> None:
        for project in ("billing_api", "site.v2"):
            with self.subTest(project=project):
                jsonl = self.transcript(self.root / project)
                self.assertEqual(self.discover([], [project]), [(project, jsonl)])


class ProjectIdTests(_Tree):
    def test_encoded_folder_maps_to_project_with_underscore(self) -> None:
        (self.root / "billing").mkdir()
        (self.root / "billing_api").mkdir()
        worktree = self.root / "billing_api" / ".xo" / "worktrees" / "thread-x"
        with patch.object(_project_encoding, "xo_projects_root", return_value=self.root):
            self.assertEqual(_project_encoding.project_id_for_encoded_cwd(encode_path(str(worktree))), "billing_api")
            self.assertEqual(_project_encoding.project_id_for_encoded_cwd(encode_path(str(self.root / "billing"))),
                             "billing")

    def test_worktree_cwd_maps_to_the_project_not_xo(self) -> None:
        worktree = self.root / "billing_api" / ".xo" / "worktrees" / "thread-x"
        worktree.mkdir(parents=True)
        with patch.object(project_index, "xo_projects_root", return_value=self.root):
            self.assertEqual(project_index.project_id_for_cwd(str(worktree)), "billing_api")


if __name__ == "__main__":
    unittest.main()
