from __future__ import annotations

import unittest

from services.cowork_agent.project_layout import relative_path_suffix


class RelativePathSuffixTests(unittest.TestCase):
    """The helper mirrors pathlib's ``suffix`` semantics, lower-cased, so the
    BFF preview allowlist keeps matching exactly what it matched before the
    path work moved out of the router (design rule P2)."""

    def test_plain_and_nested_paths(self) -> None:
        self.assertEqual(relative_path_suffix("README.md"), ".md")
        self.assertEqual(relative_path_suffix("docs/guide/INDEX.HTML"), ".html")

    def test_multi_dot_names_keep_only_the_last_suffix(self) -> None:
        self.assertEqual(relative_path_suffix("archive.tar.gz"), ".gz")

    def test_no_suffix_cases(self) -> None:
        self.assertEqual(relative_path_suffix(""), "")
        self.assertEqual(relative_path_suffix("Makefile"), "")
        self.assertEqual(relative_path_suffix("dir.d/file"), "")
        # A dotfile's whole name is its name, not a suffix — and a trailing
        # dot is not one either. Same as pathlib.
        self.assertEqual(relative_path_suffix(".env"), "")
        self.assertEqual(relative_path_suffix("notes/.hidden"), "")
        self.assertEqual(relative_path_suffix("odd."), "")

    def test_backslashes_separate_like_forward_slashes(self) -> None:
        self.assertEqual(relative_path_suffix("docs\\guide.md"), ".md")


if __name__ == "__main__":
    unittest.main()
