"""The catalog's ``peers.json`` row, checked against the **writer**.

``<project>/.xo/peers.json`` is now registered in ``quirq_catalog`` at
the **synced** tier. This file is the O-J guard for that row, and it
follows the shape ``tests/test_workitems_catalog.py`` established
because the defect it prevents is subtle:

``test_agent_docs_contract.py``'s catalog half writes one file per row
*at the location the catalog itself publishes* and then asserts the
catalog finds it — self-consistency, not agreement with reality. A row
whose tier disagreed with the real writer would still pass, because the
test would have created the file in the wrong place too. That is **O-J**
in ``docs/OUTSTANDING.md``, closed for every other row; this suite is
what stops it being reopened for this one.

So nothing here learns a location from the catalog. The document is
pinned from two independent directions, and the catalog is only ever the
thing being compared *to*:

1. **The writer's own path resolution** — ``VisualizerScope._peers_path``
   is the route layer's, and the store takes a path argument, so the
   scope is where the resolution actually lives and every route reaches
   the file through it.
2. **An observed write** — the real writer is then driven for real (add
   a peer) with the whole two-root tree snapshotted either side of the
   call, and the path from (1) has to be among the files that appeared.

Only then is the catalog asked: some published row, resolved through the
tier roots that :mod:`services.cowork_agent.project_layout` owns (T18 —
the tier decision lives there, not here and not in the catalog), must
land on exactly that absolute path.

:class:`FlippedTierTests` is the proof that any of this discriminates.
And :class:`SyncedTierTests` states the thing the tier choice actually
means: a roster is authored state that a clone would want, so it belongs
in the half of the split that travels — which is also, exactly, why
removing a peer here is a hard delete rather than a tombstone.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from services.cowork_agent import project_layout, scopes
from services.cowork_agent.quirq_catalog import (
    _TIER_RUNTIME,
    _TIER_SYNCED,
    quirq_catalog,
)


class _PeersWriterCase(unittest.TestCase):
    """A project with both roots redirected into a temp tree.

    The tree is built by hand rather than scaffolded, deliberately: the
    project template *ships* a ``peers.json`` stub, so a scaffolded
    project would already have the file and the before/after diff below
    would prove nothing about the writer.
    """

    PROJECT = "demo"
    PID = "00000001-0000-4000-8000-000000000001"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.root = tmp / "xo-projects"
        self.state = tmp / "quirq"
        self.xo = self.root / self.PROJECT / ".xo"
        self.xo.mkdir(parents=True)
        (self.xo / "project.json").write_text(
            json.dumps({
                "schema": 2,
                "pid": self.PID,
                "name": self.PROJECT,
                "created_at": "2026-01-01T00:00:00Z",
            }),
            encoding="utf-8",
        )
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.root),
            "QUIRQ_STATE_ROOT": str(self.state),
        })
        env.start()
        self.addCleanup(env.stop)
        project_layout._ROOT_RESOLUTION_CACHE.clear()
        self.addCleanup(project_layout._ROOT_RESOLUTION_CACHE.clear)

    # ── the writer ───────────────────────────────────────────────────

    def scope(self) -> scopes.VisualizerScope:
        """The handle every peers route writes through."""
        return scopes.VisualizerScope(self.PROJECT)

    def files(self) -> set[Path]:
        """Every file in the whole two-root tree, for a before/after diff."""
        found: set[Path] = set()
        for base in (self.root, self.state):
            found.update(p for p in base.rglob("*") if p.is_file())
        return found

    def sink(self) -> Path:
        """Where the peers writer really puts the document.

        Resolved by the writer, then corroborated by driving it: the
        resolver's answer must be one of the files that appeared.
        """
        resolved = self.scope()._peers_path()
        self.assertIsNotNone(
            resolved,
            "the production code could not resolve a peers path for a "
            "project that has a pid",
        )
        before = self.files()
        self.scope().create_peer(user_id="ada", role="owner")
        created = self.files() - before
        self.assertIn(
            resolved,
            created,
            f"the writer resolves {resolved} but the write created "
            f"{sorted(str(p) for p in created)}. The path helper and the "
            f"bytes disagree, so nothing downstream can be trusted.",
        )
        return resolved

    # ── the catalog, and the tier roots it names ─────────────────────

    def tier_roots(self) -> dict[str, Optional[Path]]:
        """``tier -> root``, taken from the module that owns the decision.

        The catalog names the tiers; ``project_layout`` is what a tier
        *means* on disk (T18's chokepoint). Reading the roots from there
        keeps the comparison honest — a wrong tier in a row cannot move
        the root it is checked against.
        """
        return {
            _TIER_SYNCED: project_layout.xo_dir(self.PROJECT),
            _TIER_RUNTIME: project_layout.runtime_dir_for_project(self.PROJECT),
        }

    def project_rows(self) -> list[dict]:
        return quirq_catalog()["project_outputs"]["project_contract"]

    def row_published_at(self, sink: Path) -> dict:
        """The catalog row whose tier and path resolve to ``sink``.

        Fails if none does — which is what a missing registration and a
        wrong tier both look like from the writer's side.
        """
        roots = self.tier_roots()
        rows = self.project_rows()
        matches = [
            row
            for row in rows
            if roots.get(row["tier"]) is not None
            and roots[row["tier"]] / row["path"] == sink
        ]
        self.assertEqual(
            len(matches),
            1,
            f"the catalog publishes {len(matches)} rows resolving to {sink}, "
            f"which is where a real writer just wrote. Published rows resolve "
            f"to: "
            + ", ".join(
                str(roots[r["tier"]] / r["path"])
                for r in rows
                if roots.get(r["tier"]) is not None
            ),
        )
        return matches[0]


class WriterAgreementTests(_PeersWriterCase):
    def test_the_roster_is_published_where_the_store_writes_it(self) -> None:
        row = self.row_published_at(self.sink())
        self.assertEqual(row["tier"], _TIER_SYNCED)
        self.assertEqual(row["path"], "peers.json")

    def test_the_published_location_names_the_root_the_writer_used(self) -> None:
        """``location`` is the string the UI prints. A right
        ``present_count`` under a wrong location is a different lie, not a
        fixed one."""
        sink = self.sink()
        row = self.row_published_at(sink)
        roots = self.tier_roots()
        observed = next(
            tier for tier, base in roots.items()
            if base is not None and base in sink.parents
        )
        self.assertEqual(observed, _TIER_SYNCED)
        self.assertEqual(
            row["location"],
            f"<project>/.xo/{sink.relative_to(roots[observed]).as_posix()}",
        )

    def test_the_producer_names_the_api_rather_than_a_watcher_sink(self) -> None:
        """The ``producer`` string is read by a human deciding who to
        blame for a stale file. There is no watcher peers sink and there
        never was one — these routes are the file's first writer — so
        naming one would send them to a module that does not exist."""
        row = next(r for r in self.project_rows() if r["path"] == "peers.json")
        self.assertIn("Peers API", row["producer"])
        self.assertNotIn("sink", row["producer"].lower())


class PresenceTests(_PeersWriterCase):
    """The user-visible half: ``quirq.js`` renders ``present_count``
    verbatim, so a row the writer never satisfies reads as "you have no
    data"."""

    def test_a_real_write_makes_the_row_read_as_present(self) -> None:
        row = self.row_published_at(self.sink())
        self.assertEqual(row["present_count"], 1)
        self.assertIsNotNone(row["updated_at"])
        listed = quirq_catalog()["project_outputs"]["projects"][0]["watcher_files"]
        self.assertIn("peers.json", listed)

    def test_an_unwritten_document_reads_as_absent(self) -> None:
        """The other half of the same claim: ``present_count`` counts the
        file, not the row. Without it the row could pass by always saying
        1 — and this project is built without the template, so nothing has
        put a stub there."""
        row = next(r for r in self.project_rows() if r["path"] == "peers.json")
        self.assertEqual(row["present_count"], 0)
        self.assertIsNone(row["updated_at"])


class FlippedTierTests(_PeersWriterCase):
    """Proof that the agreement check above has teeth.

    O-J is not "the test is missing" — it is "the test passes either
    way". So this asserts the counterfactual directly: reading the row at
    the *other* tier lands somewhere no writer wrote and nothing exists,
    which is exactly the "0 present" the row would render if its tier
    were flipped in the catalog.
    """

    def test_reading_the_row_at_the_other_tier_finds_nothing(self) -> None:
        sink = self.sink()
        row = self.row_published_at(sink)
        roots = self.tier_roots()
        other = _TIER_RUNTIME if row["tier"] == _TIER_SYNCED else _TIER_SYNCED
        flipped = roots[other] / row["path"]
        self.assertNotEqual(flipped, sink)
        self.assertFalse(
            flipped.exists(),
            "peers.json exists under both tiers, so a wrong tier would pass "
            "unnoticed — the check is vacuous",
        )


class SyncedTierTests(_PeersWriterCase):
    """What the tier choice *means*, rather than where the file sits.

    A roster is authored state a clone would want, so it belongs in the
    half of the split that travels — and that is precisely the fact that
    decided the hard delete: a tombstone here would carry a removed
    collaborator to every Space the project reaches.
    """

    def test_the_roster_lands_in_the_project_tree_and_nowhere_else(self) -> None:
        """The only thing a peer write may leave in the runtime tier is
        the advisory lock, which is per-machine coordination state and
        lives under ``~/.quirq/watcher/locks/`` precisely so it never
        joins the synced document it guards (``flock.py``)."""
        def runtime_files() -> set[Path]:
            if not self.state.exists():
                return set()
            return {p for p in self.state.rglob("*") if p.is_file()}

        before = runtime_files()
        self.scope().create_peer(user_id="ada", role="owner")
        self.assertTrue((self.xo / "peers.json").is_file())

        new_runtime = runtime_files() - before
        self.assertEqual(
            {p for p in new_runtime if p.suffix != ".lock"},
            set(),
            "adding a peer wrote non-lock state into the runtime tier; the "
            "roster is authored state and belongs only in the half that "
            "travels",
        )
        # And the lock is where flock.py promises it is: outside .xo/, so
        # an agent reading the synced tier never trips over a sentinel and
        # a snapshot never carries one.
        for lock in new_runtime:
            self.assertNotIn(self.xo, lock.parents)
            self.assertIn("locks", lock.parts)

    def test_a_delete_leaves_nothing_in_the_document_that_travels(self) -> None:
        scope = self.scope()
        scope.create_peer(user_id="ada", role="owner")
        scope.create_peer(user_id="grace", role="viewer")
        self.assertTrue(scope.delete_peer("ada"))
        text = (self.xo / "peers.json").read_text(encoding="utf-8")
        self.assertNotIn("ada", text)
        self.assertNotIn("deleted", text)


if __name__ == "__main__":
    unittest.main()
