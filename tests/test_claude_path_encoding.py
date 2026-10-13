"""Claude Code names a session's transcript folder by its cwd with every
non-alphanumeric character replaced by ``-``. A thread running in a worktree
under ``<project>/.claude/worktrees/`` has a ``/.`` in its cwd, which becomes
``--``; replacing only ``/`` would look in the wrong folder."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.claude_code import _project_encoding, sessions


class EncodePathTests(unittest.TestCase):
    def test_every_non_alphanumeric_character_becomes_a_hyphen(self) -> None:
        self.assertEqual(_project_encoding.encode_path("/home/coder/blogs/quirq_ai"),
                         "-home-coder-blogs-quirq-ai")
        self.assertEqual(
            _project_encoding.encode_path("/home/coder/proj-lab/.claude/worktrees/thread-cmsg_01Ab"),
            "-home-coder-proj-lab--claude-worktrees-thread-cmsg-01Ab",
        )


class NativeFileTests(unittest.TestCase):
    def test_a_worktree_sessions_transcript_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            cwd = "/xo/acs/.claude/worktrees/thread-cmsg_x1"
            folder = Path(home) / ".claude" / "projects" / "-xo-acs--claude-worktrees-thread-cmsg-x1"
            folder.mkdir(parents=True)
            (folder / "abc.jsonl").write_text("{}\n")
            with mock.patch.object(Path, "home", return_value=Path(home)):
                self.assertEqual(sessions._native_file("abc", cwd), folder / "abc.jsonl")


class ProjectForEncodedCwdTests(unittest.TestCase):
    def test_project_names_with_underscores_and_worktree_subdirs_match(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "my_app").mkdir()
            (Path(root) / "my").mkdir()
            encoded = _project_encoding.encode_path(f"{root}/my_app/.claude/worktrees/thread-1")
            with mock.patch.object(_project_encoding, "xo_projects_root", return_value=Path(root)):
                self.assertEqual(_project_encoding.project_id_for_encoded_cwd(encoded), "my_app")


if __name__ == "__main__":
    unittest.main()
