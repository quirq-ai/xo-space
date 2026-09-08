"""The catalog's three workitems rows, checked against the **writers**.

W11 registers three documents in ``quirq_catalog``:

* ``<project>/.xo/workitems.json`` — **synced**, authored work
* ``~/.quirq/projects/<pid>/github/issues.json`` — **runtime**, the mirror
* ``~/.quirq/projects/<pid>/workitems/claims.json`` — **runtime**, live claims

**Why this file exists and is not simply three more rows in
``test_agent_docs_contract.py``.** That suite's catalog half writes one
file per row *at the location the catalog itself publishes* and then
asserts the catalog finds it — which is self-consistency, not agreement
with reality. ``docs/OUTSTANDING.md`` records it as **O-J**: a row whose
tier disagreed with the real sink would still pass, because the test
would have created the file in the wrong place too. The catalog happens
to be right today; nothing was watching whether it stayed right.

So nothing here is allowed to learn a location from the catalog. Each
document is pinned from two independent directions, and the catalog is
only ever the thing being compared *to*:

1. **The writer's own path resolution.** ``github_mirror.mirror_path``
   is the poller's; ``VisualizerScope._workitems_path`` and
   ``._claims_path`` are the route layer's — the store functions take a
   path argument, so the scope is where the resolution actually lives
   and every route reaches the file through it.
2. **An observed write.** The real writer is then driven for real
   (create a workitem, record a poll, claim a workitem) with the whole
   two-root tree snapshotted either side of the call, and the path from
   (1) has to be among the files that appeared. A resolver that answered
   confidently while the bytes landed elsewhere would fail here.

Only then is the catalog asked: some published row, resolved through the
tier roots that :mod:`services.cowork_agent.project_layout` owns (T18 —
the tier decision lives there, not here and not in the catalog), must
land on exactly that absolute path.

:class:`FlippedTierTests` is the proof that any of this discriminates:
it shows the same row read at the other tier resolves somewhere no
writer ever wrote, so a mis-tiered row fails rather than passing
vacuously.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import patch

from services.cowork_agent import project_layout, scopes
from services.cowork_agent.quirq_catalog import (
    _TIER_RUNTIME,
    _TIER_SYNCED,
    quirq_catalog,
)
from services.cowork_agent.visualizer import github_mirror


@dataclass(frozen=True)
class Document:
    """One document, described only by how its *writer* is reached.

    ``resolve`` returns the path the production code computes before it
    writes; ``write`` performs a real write through the public API. No
    field here names a tier or a root — that is the catalog's claim, and
    the point of the suite is to check it rather than repeat it.
    """

    label: str
    resolve: Callable[["_WorkitemsWriterCase"], Optional[Path]]
    write: Callable[["_WorkitemsWriterCase"], None]


def _resolve_workitems(case: "_WorkitemsWriterCase") -> Optional[Path]:
    return case.scope()._workitems_path()


def _write_workitem(case: "_WorkitemsWriterCase") -> None:
    case.workitem_id = case.scope().create_workitem(
        runtime="test_runtime", title="a workitem"
    )["id"]


def _resolve_mirror(case: "_WorkitemsWriterCase") -> Optional[Path]:
    return github_mirror.mirror_path(case.PROJECT)


def _write_mirror(case: "_WorkitemsWriterCase") -> None:
    # An empty page list is a legitimate poll of a quiet repo and still
    # refreshes the document, so the mirror can be exercised end to end
    # without inventing a GitHub response.
    case.assertTrue(
        github_mirror.record_pages(
            case.PROJECT, repo="owner/repo", pages=[], complete=False
        ),
        "the poller reported that it wrote nothing",
    )


def _resolve_claims(case: "_WorkitemsWriterCase") -> Optional[Path]:
    return case.scope()._claims_path()


def _write_claim(case: "_WorkitemsWriterCase") -> None:
    if case.workitem_id is None:
        _write_workitem(case)
    case.scope().claim_workitem(
        case.workitem_id, session_id="sess-a", runtime="test_runtime"
    )


DOCUMENTS = (
    Document("workitems", _resolve_workitems, _write_workitem),
    Document("github issue mirror", _resolve_mirror, _write_mirror),
    Document("workitem claims", _resolve_claims, _write_claim),
)


class _WorkitemsWriterCase(unittest.TestCase):
    """A scaffolded project with both roots redirected into a temp tree."""

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
            json.dumps(
                {
                    "schema": 2,
                    "pid": self.PID,
                    "name": self.PROJECT,
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.root),
                "QUIRQ_STATE_ROOT": str(self.state),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        project_layout._ROOT_RESOLUTION_CACHE.clear()
        self.addCleanup(project_layout._ROOT_RESOLUTION_CACHE.clear)
        self.workitem_id: Optional[str] = None

    # ── the writers ──────────────────────────────────────────────────

    def scope(self) -> scopes.VisualizerScope:
        """The handle every workitems route writes through."""
        return scopes.VisualizerScope(self.PROJECT)

    def files(self) -> set[Path]:
        """Every file in the whole two-root tree, for a before/after diff."""
        found: set[Path] = set()
        for base in (self.root, self.state):
            found.update(p for p in base.rglob("*") if p.is_file())
        return found

    def sink_of(self, document: Document) -> Path:
        """Where ``document``'s writer really put it.

        Resolved by the writer, then corroborated by driving it: the
        resolver's answer must be one of the files that appeared.
        """
        resolved = document.resolve(self)
        self.assertIsNotNone(
            resolved,
            f"{document.label}: the production code could not resolve a path "
            f"for a project that has a pid",
        )
        before = self.files()
        document.write(self)
        created = self.files() - before
        self.assertIn(
            resolved,
            created,
            f"{document.label}: the writer resolves {resolved} but the write "
            f"created {sorted(str(p) for p in created)}. The path helper and "
            f"the bytes disagree, so nothing downstream can be trusted.",
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


class WriterAgreementTests(_WorkitemsWriterCase):
    """Each new row is published where its writer actually writes."""

    def test_the_workitems_document_is_published_where_the_store_writes_it(self) -> None:
        row = self.row_published_at(self.sink_of(DOCUMENTS[0]))
        self.assertEqual(row["tier"], _TIER_SYNCED)
        self.assertEqual(row["path"], "workitems.json")

    def test_the_mirror_is_published_where_the_poller_writes_it(self) -> None:
        row = self.row_published_at(self.sink_of(DOCUMENTS[1]))
        self.assertEqual(row["tier"], _TIER_RUNTIME)
        self.assertEqual(row["path"], "github/issues.json")

    def test_the_claims_document_is_published_where_the_claim_api_writes_it(self) -> None:
        row = self.row_published_at(self.sink_of(DOCUMENTS[2]))
        self.assertEqual(row["tier"], _TIER_RUNTIME)
        self.assertEqual(row["path"], "workitems/claims.json")

    def test_the_published_location_names_the_root_the_writer_used(self) -> None:
        """``location`` is the string the UI prints. A right ``present_count``
        under a wrong location is a different lie, not a fixed one."""
        prefixes = {
            _TIER_SYNCED: "<project>/.xo",
            _TIER_RUNTIME: "<quirq state>/projects/<pid>",
        }
        roots = self.tier_roots()
        for document in DOCUMENTS:
            with self.subTest(document=document.label):
                sink = self.sink_of(document)
                row = self.row_published_at(sink)
                # Which root the bytes are actually under decides the
                # prefix; the row only has to agree with that.
                observed = next(
                    tier
                    for tier, base in roots.items()
                    if base is not None and base in sink.parents
                )
                self.assertEqual(
                    row["location"],
                    f"{prefixes[observed]}/{sink.relative_to(roots[observed]).as_posix()}",
                )


class PresenceTests(_WorkitemsWriterCase):
    """The user-visible half: ``quirq.js`` renders ``present_count`` verbatim,
    so a row the writer never satisfies reads as "you have no data"."""

    def test_a_real_write_makes_every_new_row_read_as_present(self) -> None:
        sinks = {document.label: self.sink_of(document) for document in DOCUMENTS}
        for label, sink in sinks.items():
            with self.subTest(document=label):
                row = self.row_published_at(sink)
                self.assertEqual(
                    row["present_count"],
                    1,
                    f"{label} exists at {sink} and still renders '0 present'",
                )
                self.assertIsNotNone(row["updated_at"])
        # The rows are per-project state, so they must also show up in the
        # project's own file list rather than only in the rollup.
        listed = quirq_catalog()["project_outputs"]["projects"][0]["watcher_files"]
        for path in ("workitems.json", "github/issues.json", "workitems/claims.json"):
            self.assertIn(path, listed)

    def test_an_unwritten_document_reads_as_absent(self) -> None:
        """The other half of the same claim: ``present_count`` counts the
        file, not the row. Without it a row could pass by always saying 1."""
        for row in self.project_rows():
            if row["path"] in (
                "workitems.json",
                "github/issues.json",
                "workitems/claims.json",
            ):
                with self.subTest(path=row["path"]):
                    self.assertEqual(row["present_count"], 0)
                    self.assertIsNone(row["updated_at"])


class FlippedTierTests(_WorkitemsWriterCase):
    """Proof that the agreement check above has teeth.

    O-J is not "the test is missing" — it is "the test passes either
    way". So this asserts the counterfactual directly: reading each new
    row at the *other* tier lands somewhere no writer wrote and nothing
    exists, which is exactly the "0 present" the row would render if its
    tier were flipped in the catalog.
    """

    def test_reading_a_row_at_the_other_tier_finds_nothing(self) -> None:
        roots = self.tier_roots()
        for document in DOCUMENTS:
            with self.subTest(document=document.label):
                sink = self.sink_of(document)
                row = self.row_published_at(sink)
                other = (
                    _TIER_RUNTIME if row["tier"] == _TIER_SYNCED else _TIER_SYNCED
                )
                flipped = roots[other] / row["path"]
                self.assertNotEqual(flipped, sink)
                self.assertFalse(
                    flipped.exists(),
                    f"{document.label} exists under both tiers, so a wrong "
                    f"tier would pass unnoticed — the check is vacuous",
                )

    def test_the_two_runtime_documents_never_reach_the_synced_tier(self) -> None:
        """R-TIER, stated as the thing a snapshot would carry. A poll every
        60 s and a per-claim write in ``.xo/`` is what workitems-plan §3
        exists to prevent."""
        # The workitem itself is authored state and *does* belong in the
        # synced tier, so it is created before the snapshot — what is on
        # trial here is the mirror and the claim.
        _write_workitem(self)
        before = {p for p in self.xo.rglob("*")}
        for document in (DOCUMENTS[1], DOCUMENTS[2]):
            document.write(self)
        self.assertEqual(
            {p for p in self.xo.rglob("*")} - before,
            set(),
            "the mirror or a claim wrote into the synced tier",
        )


if __name__ == "__main__":
    unittest.main()
