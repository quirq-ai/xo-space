"""Coder-derived identity — ``space_id`` and ``owner_user_id``.

Two records changed shape on 2026-09-08:

* ``space.json:space_id`` became ``<owner>:<workspace>_<last6>`` instead of the
  bare ``CODER_WORKSPACE_ID``, **reversing syncplan O1's "captured, never
  minted"**. O1's real concern — losing the externally assigned id — is met by
  persisting it verbatim as ``coder_workspace_id``, and that is asserted here
  rather than left to inspection.
* ``project.json:owner_user_id`` resolves through Coder instead of falling
  straight to ``"local"``, and the ``"local"`` placeholder is upgraded exactly
  once. A *real* stored owner is never touched, in either record.

Every test sets the Coder variables explicitly. None may read the developer's
own environment — see ``_env`` in ``test_space_record.py`` for why.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import coder_identity
from services.cowork_agent.visualizer.sinks import project_json

_CODER = {
    "CODER_WORKSPACE_ID": "f5ae85d6-e000-4b17-892b-c3f79807c611",
    "CODER_WORKSPACE_NAME": "collabse",
    "CODER_WORKSPACE_OWNER_NAME": "ankitdwivedi",
}


def _env(**over: str) -> dict:
    return {**_CODER, **over}


class SpaceIdCompositionTests(unittest.TestCase):
    def test_the_documented_format(self) -> None:
        with patch.dict(os.environ, _env(), clear=False):
            self.assertEqual(
                coder_identity.space_id(), "ankitdwivedi:collabse_07c611"
            )

    def test_the_suffix_is_the_last_six_of_the_workspace_id(self) -> None:
        with patch.dict(os.environ, _env(), clear=False):
            self.assertTrue(
                coder_identity.space_id().endswith(
                    _CODER["CODER_WORKSPACE_ID"][-6:]
                ),
                "the link back to the assigned id must survive in the composite",
            )

    def test_a_partial_environment_yields_the_raw_id_not_a_half_formed_one(self) -> None:
        """A composite with an empty half would read as a real id."""
        for missing in ("CODER_WORKSPACE_OWNER_NAME", "CODER_WORKSPACE_NAME"):
            with self.subTest(missing=missing):
                with patch.dict(os.environ, _env(**{missing: ""}), clear=False):
                    self.assertEqual(
                        coder_identity.space_id(), _CODER["CODER_WORKSPACE_ID"]
                    )

    def test_off_coder_is_null(self) -> None:
        with patch.dict(os.environ, _env(CODER_WORKSPACE_ID=""), clear=False):
            self.assertIsNone(coder_identity.space_id())


class OwnerResolutionTests(unittest.TestCase):
    def test_coder_owner_is_used_when_unauthenticated(self) -> None:
        with patch.dict(os.environ, _env(), clear=False):
            with patch("routers.auth.auth.get_auth_state", return_value={}):
                self.assertEqual(coder_identity.resolve_user_id(), "ankitdwivedi")

    def test_the_auth_state_outranks_coder(self) -> None:
        """The authenticated id identifies a person to the wider system; the
        Coder username only within this deployment."""
        with patch.dict(os.environ, _env(), clear=False):
            with patch(
                "routers.auth.auth.get_auth_state", return_value={"user_id": "u-1234"}
            ):
                self.assertEqual(coder_identity.resolve_user_id(), "u-1234")

    def test_local_remains_the_last_resort(self) -> None:
        with patch.dict(
            os.environ, _env(CODER_WORKSPACE_OWNER_NAME=""), clear=False
        ):
            with patch("routers.auth.auth.get_auth_state", return_value={}):
                self.assertEqual(coder_identity.resolve_user_id(), "local")


class OwnerUpgradeTests(unittest.TestCase):
    """``"local"`` is upgraded once; a real owner is never reassigned."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _project(self, owner) -> Path:
        xo = self.root / "demo" / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text(
            json.dumps(
                {
                    "schema": 2,
                    "pid": "00000001-0000-4000-8000-000000000001",
                    "name": "demo",
                    "owner_user_id": owner,
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        return xo

    def _owner_after_fill(self, xo: Path, **kw) -> str:
        with patch.dict(os.environ, _env(), clear=False):
            with patch("routers.auth.auth.get_auth_state", return_value={}):
                project_json.fill_identity(xo, "demo", **kw)
        return json.loads((xo / "project.json").read_text("utf-8"))["owner_user_id"]

    def test_local_is_upgraded_even_though_the_pid_is_already_minted(self) -> None:
        """The early return is what would otherwise freeze "local" forever:
        the fill never runs again on a project that has a pid."""
        self.assertEqual(self._owner_after_fill(self._project("local")), "ankitdwivedi")

    def test_a_real_owner_is_never_reassigned(self) -> None:
        self.assertEqual(self._owner_after_fill(self._project("u-1234")), "u-1234")

    def test_migrate_opts_out_and_leaves_the_file_untouched(self) -> None:
        """``migrate`` calls the fill only to mint a pid; its contract is that
        the synced tree is not rewritten."""
        xo = self._project("local")
        before = (xo / "project.json").read_bytes()
        self._owner_after_fill(xo, upgrade_placeholder_owner=False)
        self.assertEqual((xo / "project.json").read_bytes(), before)

    def test_no_upgrade_when_there_is_nothing_better(self) -> None:
        """Off Coder and unauthenticated, "local" stays — the fill must not
        rewrite "local" over "local" on every tick."""
        xo = self._project("local")
        before = (xo / "project.json").read_bytes()
        with patch.dict(
            os.environ, _env(CODER_WORKSPACE_OWNER_NAME=""), clear=False
        ):
            with patch("routers.auth.auth.get_auth_state", return_value={}):
                project_json.fill_identity(xo, "demo")
        self.assertEqual((xo / "project.json").read_bytes(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
