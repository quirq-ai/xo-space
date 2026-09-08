"""W6 — the GitHub issue mirror, and the tier it is not allowed to leave.

``docs/workitems-plan.md`` §5.2 and §10 (W6). Three groups of assertion carry
the weight, and each corresponds to a way this document could be quietly
wrong rather than loudly broken:

* **Tier.** The mirror is re-fetched every 60 seconds. In ``.xo/`` that would
  make the synced tier churn at GitHub's rate — the thing syncplan T19/T20
  removed — and hand A10 a document that conflicts on every poll. So the W6
  acceptance row demands it in as many words: *nothing is written to ``.xo/``
  by the poller (asserted)*. :class:`TierTests` snapshots the whole projects
  root across a write and compares it byte for byte.

* **The merge.** §6.3 amendment 6 records a real design bug: with a
  high-water mark and ``states: [OPEN]``, an issue closed since the mark stops
  appearing at all, so an incremental merge leaves a stale ``open`` row in the
  mirror forever. :class:`StrandingTests` is that scenario, start to finish.

* **Degradation.** A failed poll is not evidence that the issues went away.
  Every failure case here asserts the last good rows survived it.

Everything runs offline and unauthenticated: not one test in this file spawns
``gh`` or touches the network.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:  # pragma: no cover - exercised by its absence, not by a branch
    from jsonschema import Draft7Validator
except ImportError:  # pragma: no cover
    Draft7Validator = None  # type: ignore[assignment]

from services.cowork_agent.connectors.github_issues import IssuesResult, RateLimit
from services.cowork_agent.visualizer import github_mirror

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    ROOT / "services" / "cowork_agent" / "visualizer" / "schema"
    / "github-issues.schema.json"
)

_SKIP_JSONSCHEMA = (
    "jsonschema is not installed — "
    "uv pip install --python venv/bin/python -r requirements-dev.txt"
)

PID = "11111111-2222-4333-8444-555555555555"
REPO = "dwivedi-ai/xo-cowork-api"
OTHER_REPO = "dwivedi-ai/other"


def _row(node_id: str, number: int, *, state: str = "open",
         updated_at: str = "2026-09-08T12:00:00Z", **extra) -> dict:
    row = {
        "node_id": node_id,
        "number": number,
        "title": f"Issue {number}",
        "state": state,
        "state_reason": None,
        "assignees": [],
        "url": f"https://github.com/{REPO}/issues/{number}",
        "updated_at": updated_at,
    }
    row.update(extra)
    return row


def _ok(rows, *, has_next=False, cursor=None, cost=1, remaining=4900) -> IssuesResult:
    return IssuesResult(
        ok=True,
        repo=REPO,
        fetched_at="2026-09-08T12:00:00Z",
        issues=list(rows),
        rate=RateLimit(
            limit=5000, cost=cost, remaining=remaining,
            reset_at="2026-09-08T13:00:00Z",
        ),
        has_next_page=has_next,
        end_cursor=cursor,
    )


def _failed(kind: str, message: str = "boom", *, rate: RateLimit | None = None) -> IssuesResult:
    return IssuesResult(
        ok=False,
        repo=REPO,
        fetched_at="2026-09-08T12:05:00Z",
        rate=rate or RateLimit(),
        error_kind=kind,
        error=message,
    )


class _MirrorCase(unittest.TestCase):
    """A temp workspace with one scaffolded, pid-bearing project."""

    PROJECT = "demo"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "xo-projects"
        self.state = self.tmp / "state"
        (self.root / self.PROJECT / ".xo").mkdir(parents=True)
        self.state.mkdir(parents=True)
        (self.root / self.PROJECT / ".xo" / "project.json").write_text(
            json.dumps({"name": self.PROJECT, "pid": PID,
                        "git": {"remote_url": f"https://github.com/{REPO}.git"}}),
            encoding="utf-8",
        )
        env = patch.dict(
            os.environ,
            {"XO_PROJECTS_ROOT": str(self.root), "QUIRQ_STATE_ROOT": str(self.state)},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    # ── helpers ──────────────────────────────────────────────────────────
    def doc(self) -> dict:
        loaded = github_mirror.read_mirror(self.PROJECT)
        self.assertIsNotNone(loaded, "expected a mirror document on disk")
        return loaded  # type: ignore[return-value]

    def snapshot(self, base: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in sorted(base.rglob("*")):
            if path.is_file():
                out[str(path.relative_to(base))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            elif path.is_dir():
                out[str(path.relative_to(base)) + "/"] = "dir"
        return out


# ── Tier ─────────────────────────────────────────────────────────────────────


class TierTests(_MirrorCase):
    """R-TIER, checked dynamically rather than by convention (W6's row)."""

    def test_the_mirror_lives_under_the_runtime_root(self) -> None:
        path = github_mirror.mirror_path(self.PROJECT)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(
            path, self.state / "projects" / PID / "github" / "issues.json"
        )

    def test_the_path_is_keyed_by_pid_and_never_names_the_synced_tier(self) -> None:
        """Keyed by ``project.json:pid``, not by the folder name — and the
        string contains no ``.xo`` anywhere, which is the cheap check that
        catches a hand-built path a helper was supposed to own (T18)."""
        path = github_mirror.mirror_path(self.PROJECT)
        assert path is not None
        self.assertIn(PID, str(path))
        self.assertNotIn(".xo", str(path))
        self.assertNotIn(str(self.root), str(path))

    def test_a_write_touches_nothing_under_the_projects_root(self) -> None:
        """The W6 acceptance criterion, literally: *nothing is written to
        ``.xo/`` by the poller*. Compared by content hash rather than by
        mtime, because a rewrite with identical bytes is still a write into a
        tier that is snapshot-restored wholesale."""
        before = self.snapshot(self.root)
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        github_mirror.record_failure(
            self.PROJECT, repo=REPO, result=_failed("network")
        )
        self.assertEqual(self.snapshot(self.root), before)

    def test_the_document_appears_under_the_runtime_root(self) -> None:
        """The other half: it did not land in ``.xo/`` *and* it did land
        somewhere. A test that only asserts absence passes when the writer is
        broken."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        self.assertIn(
            "projects/" + PID + "/github/issues.json",
            [key for key in self.snapshot(self.state)],
        )

    def test_removing_the_runtime_root_repopulates_on_the_next_poll(self) -> None:
        """``rm -rf ~/.quirq`` is a documented clean reset (syncplan §4)."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        path = github_mirror.mirror_path(self.PROJECT)
        assert path is not None
        shutil.rmtree(self.state)
        self.assertIsNone(github_mirror.read_mirror(self.PROJECT))
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        self.assertEqual(list(self.doc()["issues"]), ["I_1"])

    def test_a_project_that_does_not_exist_resolves_to_nothing(self) -> None:
        """Skip, don't conjure: a runtime home for a ghost project is how a
        poller invents a directory nobody asked for."""
        self.assertIsNone(github_mirror.mirror_path("no-such-project"))
        self.assertIsNone(github_mirror.read_mirror("no-such-project"))
        self.assertFalse(
            github_mirror.record_pages(
                "no-such-project", repo=REPO, pages=[_ok([])], complete=True
            )
        )


# ── The document ─────────────────────────────────────────────────────────────


@unittest.skipIf(Draft7Validator is None, _SKIP_JSONSCHEMA)
class DocumentShapeTests(_MirrorCase):
    """What the writer actually produces, checked against the schema that
    governs it — the T16 principle: a schema nothing validates drifts."""

    def setUp(self) -> None:
        super().setUp()
        self.validator = Draft7Validator(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        )

    def test_a_seeded_document_validates(self) -> None:
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([
                _row("I_1", 1),
                _row("I_2", 2, state="closed", state_reason="completed",
                     assignees=[{"login": "peer", "avatar_url": None}]),
            ])],
            complete=True,
        )
        self.validator.validate(self.doc())

    def test_a_failure_document_validates(self) -> None:
        github_mirror.record_failure(
            self.PROJECT, repo=REPO, result=_failed("not_authenticated", "connect me")
        )
        self.validator.validate(self.doc())

    def test_an_empty_mirror_validates(self) -> None:
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([])], complete=True
        )
        self.validator.validate(self.doc())

    def test_the_document_carries_no_etag(self) -> None:
        """§13 amendment 3: a REST idea that does not survive D5. The schema
        still declares it so the plan's literal example validates, but nothing
        writes it — GraphQL has no conditional request."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([])], complete=True
        )
        self.assertNotIn("etag", self.doc())

    def test_rows_carry_no_labels_key(self) -> None:
        """§5.2 amendment 2: absent, never ``[]``. An empty array asserts
        'this issue has no labels', which the poll never establishes — it does
        not fetch them, because ``labels(first:10)`` doubles the query cost."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        self.assertNotIn("labels", self.doc()["issues"]["I_1"])

    def test_issues_are_keyed_by_node_id(self) -> None:
        """Unique by construction and rename-proof — the O-C lesson. ``number``
        is per-repo and would collide the moment two repos meet in one map."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([_row("I_1", 1), _row("I_2", 2)])], complete=True,
        )
        self.assertEqual(sorted(self.doc()["issues"]), ["I_1", "I_2"])

    def test_the_rate_is_the_newest_reading_with_the_polls_total_cost(self) -> None:
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[
                _ok([_row("I_1", 1)], cost=1, remaining=4900, has_next=True, cursor="c"),
                _ok([_row("I_2", 2)], cost=1, remaining=4899),
            ],
            complete=True,
        )
        self.assertEqual(
            self.doc()["rate"],
            {"remaining": 4899, "reset_at": "2026-09-08T13:00:00Z",
             "limit": 5000, "cost": 2},
        )

    def test_an_unobserved_rate_is_null_not_a_half_object(self) -> None:
        github_mirror.record_failure(
            self.PROJECT, repo=REPO, result=_failed("no_cli", "gh missing")
        )
        self.assertIsNone(self.doc()["rate"])
        self.validator.validate(self.doc())


# ── The merge, and the stranding bug it exists to fix ────────────────────────


class StrandingTests(_MirrorCase):
    """§6.3 amendment 6 — the one this module was written for.

    "With ``since`` set and ``states: [OPEN]``, an issue closed since the mark
    simply stops appearing… an incremental merge therefore leaves a stale
    ``open`` row in the mirror **forever**." Two mechanisms fix it and both
    are asserted here: the incremental poll can carry a ``closed`` row at all
    (the poller's half is asserted in ``test_github_poller.py``), and a
    complete seed replaces the map outright.
    """

    def test_a_closed_row_overwrites_the_stale_open_one(self) -> None:
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([_row("I_1", 1, state="open",
                             updated_at="2026-09-08T10:00:00Z")])],
            complete=True,
        )
        self.assertEqual(self.doc()["issues"]["I_1"]["state"], "open")

        # The steady-state poll — ``since`` is now set — sees the transition
        # only because the query asked for CLOSED as well.
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([_row("I_1", 1, state="closed", state_reason="completed",
                             updated_at="2026-09-08T11:00:00Z")])],
            complete=True,
        )
        row = self.doc()["issues"]["I_1"]
        self.assertEqual(row["state"], "closed")
        self.assertEqual(row["state_reason"], "completed")

    def test_the_closed_row_is_kept_rather_than_dropped(self) -> None:
        """"Drop or mark", says §6.3; this store marks. Dropping would put the
        adopted item back to "state unknown" one tick after we learned the
        truth, and §5.3's projection reads the mirror for state *always*."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([_row("I_1", 1, state="closed",
                             updated_at="2026-09-08T13:00:00Z")])],
            complete=True,
        )
        self.assertIn("I_1", self.doc()["issues"])

    def test_a_complete_seed_replaces_the_map_and_drops_a_stranded_row(self) -> None:
        """The repair path for a mirror written before this fix existed: the
        row is stale ``open``, no incremental poll will ever mention it again,
        and the seed is what clears it."""
        path = github_mirror.mirror_path(self.PROJECT, create=True)
        assert path is not None
        path.write_text(json.dumps({
            "schema": 1, "repo": REPO, "fetched_at": "2026-09-01T00:00:00Z",
            "since": None,
            "issues": {"I_ghost": _row("I_ghost", 99)},
        }), encoding="utf-8")

        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        self.assertEqual(list(self.doc()["issues"]), ["I_1"])

    def test_an_incremental_poll_merges_rather_than_replacing(self) -> None:
        """The other direction, and the reason the seed/steady distinction has
        to exist: a ``since``-bounded page is not the whole repository, so
        replacing on it would delete every issue that simply did not change."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([_row("I_1", 1), _row("I_2", 2)])], complete=True,
        )
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([_row("I_3", 3, updated_at="2026-09-08T14:00:00Z")])],
            complete=True,
        )
        self.assertEqual(sorted(self.doc()["issues"]), ["I_1", "I_2", "I_3"])

    def test_an_incomplete_seed_merges_so_two_truncated_seeds_accumulate(self) -> None:
        """A partial snapshot is not authoritative about what is missing.
        Replacing on it would make successive truncated seeds oscillate
        instead of converge."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=False
        )
        self.assertFalse(github_mirror.load_state(self.PROJECT, repo=REPO).seeded)
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_2", 2)])], complete=False
        )
        self.assertEqual(sorted(self.doc()["issues"]), ["I_1", "I_2"])


class HighWaterMarkTests(_MirrorCase):
    def test_a_complete_poll_sets_the_mark_to_the_newest_row(self) -> None:
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([
                _row("I_1", 1, updated_at="2026-09-08T10:00:00Z"),
                _row("I_2", 2, updated_at="2026-09-08T11:00:00Z"),
            ])],
            complete=True,
        )
        self.assertEqual(self.doc()["since"], "2026-09-08T11:00:00Z")

    def test_an_incomplete_poll_does_not_advance_the_mark(self) -> None:
        """The stranding bug arrived at from the other side. The query is
        ``UPDATED_AT DESC``, so page 1 holds the newest rows; advancing to
        them while pages remain unread would skip everything in between
        permanently."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([_row("I_1", 1, updated_at="2026-09-08T11:00:00Z")])],
            complete=True,
        )
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([_row("I_9", 9, updated_at="2026-09-08T23:00:00Z")])],
            complete=False,
        )
        self.assertEqual(self.doc()["since"], "2026-09-08T11:00:00Z")

    def test_the_mark_never_rewinds(self) -> None:
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([_row("I_1", 1, updated_at="2026-09-08T11:00:00Z")])],
            complete=True,
        )
        github_mirror.record_pages(
            self.PROJECT, repo=REPO,
            pages=[_ok([_row("I_2", 2, updated_at="2026-09-01T00:00:00Z")])],
            complete=True,
        )
        self.assertEqual(self.doc()["since"], "2026-09-08T11:00:00Z")

    def test_a_quiet_repo_with_no_rows_stays_unseeded(self) -> None:
        """Cheaper and safer than stamping ``now``: a mark taken from our own
        clock could sit ahead of GitHub's and hide an issue forever, and a
        repo with no open issues has nothing to strand. Re-seeding costs the
        same single point as an incremental poll."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([])], complete=True
        )
        self.assertIsNone(self.doc()["since"])
        self.assertFalse(github_mirror.load_state(self.PROJECT, repo=REPO).seeded)

    def test_a_changed_remote_reseeds_rather_than_mixing_two_repos(self) -> None:
        """The schema records ``repo`` so a mirror can never be read as
        belonging to a repo it did not come from. A remote can change under a
        project, and the previous repo's high-water mark means nothing in the
        new one."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        state = github_mirror.load_state(self.PROJECT, repo=OTHER_REPO)
        self.assertIsNone(state.since)
        github_mirror.record_pages(
            self.PROJECT, repo=OTHER_REPO,
            pages=[_ok([_row("I_9", 9)])], complete=True,
        )
        doc = self.doc()
        self.assertEqual(doc["repo"], OTHER_REPO)
        self.assertEqual(list(doc["issues"]), ["I_9"])


class ClosedRowBoundTests(_MirrorCase):
    def test_closed_rows_are_bounded_and_open_rows_are_never_dropped(self) -> None:
        cap = github_mirror.MAX_CLOSED_ROWS
        rows = [
            _row(f"I_c{i}", i + 1, state="closed",
                 updated_at=f"2026-01-{(i % 28) + 1:02d}T00:00:00Z")
            for i in range(cap + 5)
        ]
        rows.append(_row("I_open", 9999, updated_at="2020-01-01T00:00:00Z"))
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok(rows)], complete=True
        )
        issues = self.doc()["issues"]
        closed = [row for row in issues.values() if row["state"] == "closed"]
        self.assertEqual(len(closed), cap)
        self.assertIn("I_open", issues)


# ── Degradation ──────────────────────────────────────────────────────────────


class FailureTests(_MirrorCase):
    def test_a_failed_poll_keeps_the_last_good_rows(self) -> None:
        """A failed poll is not evidence that the issues went away."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        github_mirror.record_failure(
            self.PROJECT, repo=REPO, result=_failed("network", "dial tcp")
        )
        doc = self.doc()
        self.assertEqual(list(doc["issues"]), ["I_1"])
        self.assertEqual(doc["since"], "2026-09-08T12:00:00Z")

    def test_a_failure_does_not_move_fetched_at(self) -> None:
        """``fetched_at`` means "when the poll that produced these issues
        completed", and the UI's staleness indicator reads it. A failure
        produced none, and carries its own ``error.at`` — two different facts
        that one timestamp would collapse into a lie."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        first = self.doc()["fetched_at"]
        github_mirror.record_failure(
            self.PROJECT, repo=REPO, result=_failed("timeout")
        )
        doc = self.doc()
        self.assertEqual(doc["fetched_at"], first)
        self.assertNotEqual(doc["error"]["at"], None)

    def test_the_error_is_structured_not_a_bare_string(self) -> None:
        """§13 amendment 4. "Connect GitHub" and "the network is down" need
        different affordances, so the kind is a closed set."""
        github_mirror.record_failure(
            self.PROJECT, repo=REPO,
            result=_failed("not_authenticated", "gh auth login"),
        )
        error = self.doc()["error"]
        self.assertEqual(error["kind"], "not_authenticated")
        self.assertEqual(error["message"], "gh auth login")
        self.assertIn("at", error)

    def test_a_first_failure_creates_a_document_with_a_null_fetched_at(self) -> None:
        """Something for the UI to hang "connect GitHub" off before the first
        successful poll — the state the schema declares ``fetched_at: null``
        for."""
        github_mirror.record_failure(
            self.PROJECT, repo=REPO, result=_failed("not_authenticated")
        )
        doc = self.doc()
        self.assertIsNone(doc["fetched_at"])
        self.assertEqual(doc["issues"], {})

    def test_a_repeated_identical_failure_writes_nothing(self) -> None:
        """``error.at`` is the mirror's one volatile path. A machine with no
        GitHub auth fails identically every 60 seconds forever; without the
        mask each of those would rewrite the document to move a timestamp
        nobody is waiting on."""
        result = _failed("not_authenticated", "gh auth login")
        self.assertTrue(
            github_mirror.record_failure(self.PROJECT, repo=REPO, result=result)
        )
        self.assertFalse(
            github_mirror.record_failure(self.PROJECT, repo=REPO, result=result)
        )

    def test_a_different_failure_does_write(self) -> None:
        github_mirror.record_failure(
            self.PROJECT, repo=REPO, result=_failed("not_authenticated")
        )
        self.assertTrue(
            github_mirror.record_failure(
                self.PROJECT, repo=REPO, result=_failed("network")
            )
        )

    def test_a_success_clears_the_error(self) -> None:
        github_mirror.record_failure(
            self.PROJECT, repo=REPO, result=_failed("network")
        )
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        self.assertIsNone(self.doc()["error"])

    def test_a_failure_that_never_reached_github_keeps_the_last_budget(self) -> None:
        """No ``gh``, no network — nothing was learned about the budget.
        Keeping the last reading is honest; replacing it with null would read
        as "unknown" when it is merely unchanged."""
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        github_mirror.record_failure(
            self.PROJECT, repo=REPO, result=_failed("no_cli")
        )
        self.assertEqual(self.doc()["rate"]["remaining"], 4900)


class CorruptionTests(_MirrorCase):
    """The deliberate opposite of ``workitems_store``'s O-E refusal.

    That document is authored state in the synced tier and the bytes on disk
    may be the only copy, so it raises. This one is a cache whose loss costs
    exactly one poll, so it is repaired by overwriting — which is also what
    the schema's own description prescribes.
    """

    def _write_raw(self, text: str) -> Path:
        path = github_mirror.mirror_path(self.PROJECT, create=True)
        assert path is not None
        path.write_text(text, encoding="utf-8")
        return path

    def test_invalid_json_reads_as_absent(self) -> None:
        self._write_raw("{not json")
        self.assertIsNone(github_mirror.read_mirror(self.PROJECT))
        self.assertFalse(github_mirror.load_state(self.PROJECT, repo=REPO).exists)

    def test_an_empty_file_reads_as_absent(self) -> None:
        """A zero-byte JSON file is what a truncated non-atomic write leaves
        behind (syncplan T4), never a legitimate state."""
        self._write_raw("")
        self.assertIsNone(github_mirror.read_mirror(self.PROJECT))

    def test_a_future_schema_is_rebuilt_rather_than_half_understood(self) -> None:
        self._write_raw(json.dumps({"schema": 2, "repo": REPO, "issues": {}}))
        self.assertIsNone(github_mirror.read_mirror(self.PROJECT))

    def test_the_next_poll_repairs_a_corrupt_file(self) -> None:
        self._write_raw("{not json")
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        self.assertEqual(list(self.doc()["issues"]), ["I_1"])

    def test_a_poisoned_row_is_dropped_rather_than_carried_forward(self) -> None:
        """The document is validated against its schema by whatever reads it;
        one unusable row would fail the whole file for every consumer."""
        self._write_raw(json.dumps({
            "schema": 1, "repo": REPO, "fetched_at": "2026-09-08T10:00:00Z",
            "since": "2026-09-08T10:00:00Z",
            "issues": {
                "I_good": _row("I_good", 1),
                "I_bad": {"node_id": "I_bad"},
                "I_mismatch": _row("I_other", 3),
                "I_list": [],
            },
        }))
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_new", 2)])], complete=True
        )
        self.assertEqual(sorted(self.doc()["issues"]), ["I_good", "I_new"])


class StateTests(_MirrorCase):
    def test_an_absent_mirror_is_unseeded(self) -> None:
        state = github_mirror.load_state(self.PROJECT, repo=REPO)
        self.assertFalse(state.exists)
        self.assertFalse(state.seeded)
        self.assertIsNone(state.since)

    def test_a_complete_seed_makes_the_mirror_seeded(self) -> None:
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        state = github_mirror.load_state(self.PROJECT, repo=REPO)
        self.assertTrue(state.seeded)
        self.assertEqual(state.repo, REPO)
        self.assertEqual(state.issue_count, 1)
        self.assertIsNone(state.error)

    def test_reset_forces_the_next_poll_to_seed(self) -> None:
        github_mirror.record_pages(
            self.PROJECT, repo=REPO, pages=[_ok([_row("I_1", 1)])], complete=True
        )
        self.assertTrue(github_mirror.reset_mirror(self.PROJECT))
        self.assertFalse(github_mirror.load_state(self.PROJECT, repo=REPO).seeded)
        self.assertFalse(github_mirror.reset_mirror(self.PROJECT))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
