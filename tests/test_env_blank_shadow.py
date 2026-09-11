"""Blank exports must not shadow real ``.env`` values.

A variable exported as an empty string is still *present* in ``os.environ``,
and python-dotenv skips a key on membership rather than truthiness — so
``load_dotenv()`` alone leaves the blank in place and the file value never
lands. ``server._prune_blank_env_shadows`` closes that gap without disturbing
the documented precedence (shell > .env).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported at module scope on purpose: importing server runs its dotenv
# prologue, which is the code under test.
import server


class PruneBlankEnvShadowsTests(unittest.TestCase):
    def test_blank_export_is_dropped_when_file_has_a_value(self):
        """The reported bug: sharing parked on a blank XO_SPACE_ID."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("XO_SPACE_ID=ws-from-file\n")
            with patch.dict(os.environ, {"XO_SPACE_ID": ""}):
                server._prune_blank_env_shadows(path)
                self.assertNotIn("XO_SPACE_ID", os.environ)

    def test_whitespace_only_export_counts_as_blank(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("XO_SPACE_ID=ws-from-file\n")
            with patch.dict(os.environ, {"XO_SPACE_ID": "   "}):
                server._prune_blank_env_shadows(path)
                self.assertNotIn("XO_SPACE_ID", os.environ)

    def test_non_empty_export_still_outranks_the_file(self):
        """Precedence is unchanged: an explicit export wins, as install.sh documents."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("XO_SPACE_ID=ws-from-file\n")
            with patch.dict(os.environ, {"XO_SPACE_ID": "ws-from-shell"}):
                server._prune_blank_env_shadows(path)
                self.assertEqual(os.environ.get("XO_SPACE_ID"), "ws-from-shell")

    def test_blank_stays_when_the_file_has_nothing_to_offer(self):
        """A var using "" as an off switch keeps its blank."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("ANTHROPIC_API_KEY=\nOTHER=value\n")
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
                server._prune_blank_env_shadows(path)
                self.assertIn("ANTHROPIC_API_KEY", os.environ)
                self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "")

    def test_unset_key_is_left_unset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / ".env"
            path.write_text("XO_SPACE_ID=ws-from-file\n")
            with patch.dict(os.environ):
                os.environ.pop("XO_SPACE_ID", None)
                server._prune_blank_env_shadows(path)
                self.assertNotIn("XO_SPACE_ID", os.environ)

    def test_missing_env_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"XO_SPACE_ID": ""}):
                server._prune_blank_env_shadows(Path(temp_dir) / "absent.env")
                self.assertEqual(os.environ.get("XO_SPACE_ID"), "")

    def test_unreadable_env_file_is_not_an_error(self):
        """dotenv_values raising OSError must not stop the server booting."""
        with patch.object(server, "dotenv_values", side_effect=OSError("boom")):
            with patch.dict(os.environ, {"XO_SPACE_ID": ""}):
                server._prune_blank_env_shadows(Path("/nonexistent/.env"))
                self.assertEqual(os.environ.get("XO_SPACE_ID"), "")


if __name__ == "__main__":
    unittest.main()
