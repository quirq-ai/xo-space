"""W7/W8 — adoption, the read-time projection, and assignment.

``docs/workitems-plan.md`` §5.3, §7.2, §10 rows W7 and W8. Five things
carry the weight here, and each is a way this feature could be quietly
wrong rather than loudly broken.

* **An adopted item never 404s.** W7's acceptance criterion is that with
  the mirror deleted the item still renders — from the title and labels
  snapshotted at adoption, flagged stale, with state and assignee
  *unknown*. :class:`StaleProjectionTests` deletes the mirror and asserts
  the whole surface still answers 200. GitHub being unreachable is the
  normal case on a laptop, not an exception.

* **The projection is total.** The BFF builds its wire models *outside*
  the route's ``try``/``except``, which is deliberate and load-bearing —
  a ``ValidationError`` raised there once took a whole project's list
  with it. So :func:`workitem_projection.project_workitem` is fed
  garbage in :class:`ProjectionTotalityTests` and must degrade rather
  than raise.

* **Adoption is a state transition, not a field edit** (§13 amendment 8).
  ``source.kind`` decides which fields the record may carry at all, so
  the store has to *drop* four keys in one direction and *materialise*
  them in the other. Both directions are validated against
  ``workitems.schema.json`` here, because "the record still validates" is
  the entire reason the function exists.

* **Assignment never touches GitHub, for either kind of workitem.** D1
  put coordination in GitHub and W8 wrote an assignee onto the issue;
  that decision was reversed (§13, amendment 33), ``set_assignees`` is
  deleted, and :class:`NoGithubWriteTests` proves the claim the hard way
  — it makes the subprocess seam itself explode and then assigns both an
  adopted and a local workitem.

* **Assignment is a local annotation, accepted for anyone.** It lands in
  ``.xo/workitems.json`` for every workitem, adopted or not. The
  ``local_assignee_only`` refusal is gone with the GitHub write it was
  standing in for; what it guarded against is now true of *every*
  assignment and is documented rather than enforced — ``.xo/`` does not
  continuously sync, so an assignment is visible only inside the Space
  that made it.

Everything runs **offline and unauthenticated**: no test here spawns
``gh``, reads a real token, or touches the network. Both roots are
redirected into a temp dir, the GitHub client is stubbed at the module
boundary the router calls, and the poller's in-process interest map is
reset around every case.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:  # pragma: no cover - exercised by its absence, not by a branch
    from jsonschema import Draft7Validator
except ImportError:  # pragma: no cover
    Draft7Validator = None  # type: ignore[assignment]

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.cowork_agent import github_poller
from services.cowork_agent.connectors import github_issue_actions, github_issues
from services.cowork_agent.connectors.github_issues import (
    ERROR_KINDS,
    query_connections,
)
from services.cowork_agent.visualizer import (
    github_mirror,
    workitem_projection,
    workitems_store,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    ROOT / "services" / "cowork_agent" / "visualizer" / "schema"
    / "workitems.schema.json"
)
_SKIP_JSONSCHEMA = (
    "jsonschema is not installed — "
    "uv pip install --python venv/bin/python -r requirements-dev.txt"
)

PID = "22222222-3333-4444-8555-666666666666"
REPO = "dwivedi-ai/xo-cowork-api"
NODE_ID = "I_kwDOABCD1234"
GITHUB_REF = {
    "repo": REPO,
    "number": 42,
    "node_id": NODE_ID,
    "url": f"https://github.com/{REPO}/issues/42",
}


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _mirror_row(
    *, node_id: str = NODE_ID, number: int = 42, title: str = "Rate-limit the poller",
    state: str = "open", state_reason=None, assignees=None,
    updated_at: str = "2026-09-08T12:00:00Z", **extra,
) -> dict:
    row = {
        "node_id": node_id,
        "number": number,
        "title": title,
        "state": state,
        "state_reason": state_reason,
        "assignees": list(assignees or []),
        "url": f"https://github.com/{REPO}/issues/{number}",
        "updated_at": updated_at,
    }
    row.update(extra)
    return row


def _issue_payload(**overrides) -> dict:
    """What ``github_issue_actions.fetch_issue`` hands back on success."""
    payload = {
        "node_id": NODE_ID,
        "number": 42,
        "repo": REPO,
        "title": "Rate-limit the poller",
        "state": "open",
        "state_reason": None,
        "assignees": [],
        "labels": ["infra", "perf"],
        "url": f"https://github.com/{REPO}/issues/42",
        "updated_at": "2026-09-08T12:00:00Z",
    }
    payload.update(overrides)
    return payload


def _fetch_ok(**overrides):
    async def _fetch(repo, number, **kwargs):
        return github_issue_actions.IssueResult(
            ok=True, repo=str(repo), number=number, issue=_issue_payload(**overrides),
        )
    return _fetch


def _fetch_failing(kind: str, message: str = "boom"):
    async def _fetch(repo, number, **kwargs):
        return github_issue_actions.IssueResult(
            ok=False, repo=str(repo), number=number,
            error_kind=kind, error=message,
        )
    return _fetch


def _assign_ok(*logins: str):
    async def _assign(repo, number, wanted, **kwargs):
        return github_issue_actions.AssignResult(
            ok=True, repo=str(repo), number=number, assignees=list(logins),
        )
    return _assign


def _assign_echo():
    """GitHub's ordinary behaviour: it assigns exactly what it was given."""
    async def _assign(repo, number, wanted, **kwargs):
        return github_issue_actions.AssignResult(
            ok=True, repo=str(repo), number=number, assignees=list(wanted),
        )
    return _assign


def _assign_failing(kind: str, message: str = "boom"):
    async def _assign(repo, number, wanted, **kwargs):
        return github_issue_actions.AssignResult(
            ok=False, repo=str(repo), number=number,
            error_kind=kind, error=message,
        )
    return _assign


async def _no_login(**kwargs):
    return github_issue_actions.LoginResult(
        ok=False, error_kind="not_authenticated", error="not connected",
    )


def _login(login: str):
    async def _found(**kwargs):
        return github_issue_actions.LoginResult(ok=True, login=login)
    return _found


# ── The pure join (§5.3) ────────────────────────────────────────────────────


class ProjectionTests(unittest.TestCase):
    """The table in §5.3, one assertion per row."""

    def adopted(self, **extra) -> dict:
        record = {
            "id": "8f3a1c92-6b0e-4c11-9a3d-5e2f7c1d0a44",
            "title": "snapshotted at adoption",
            "labels": ["infra"],
            "source": {"kind": "github", "github": dict(GITHUB_REF)},
            "links": {"todo_ids": [], "session_ids": []},
        }
        record.update(extra)
        return record

    def test_state_comes_from_the_mirror_always(self) -> None:
        row = _mirror_row(state="closed", state_reason="not_planned")
        out = workitem_projection.project_workitem(
            self.adopted(), issues={NODE_ID: row}
        )
        self.assertEqual(out["status"], "closed")
        self.assertEqual(out["state_reason"], "not_planned")
        self.assertFalse(out["stale"])

    def test_the_assignee_is_the_files_and_githubs_is_served_beside_it(self) -> None:
        """§13 amendment 33, the row of the table that changed sides.
        ``assignee`` used to be "the mirror, always" for an adopted item.
        It is now ours — read from the file for both kinds — and what
        GitHub knows is served under a name that cannot be mistaken for
        it."""
        row = _mirror_row(
            assignees=[{"login": "ada", "avatar_url": "https://a"},
                       {"login": "grace", "avatar_url": None}],
        )
        out = workitem_projection.project_workitem(
            self.adopted(assignee="dwivedi-ai"), issues={NODE_ID: row}
        )
        self.assertEqual(out["assignee"], "dwivedi-ai", "ours, from the file")
        self.assertEqual(out["assignees"], ["dwivedi-ai"])
        self.assertEqual(out["github_assignees"], ["ada", "grace"],
                         "GitHub's own, flattened — information, not assignment")

    def test_an_unassigned_adopted_item_is_not_assigned_to_githubs_people(self) -> None:
        """The failure this split exists to prevent: GitHub having someone
        on the issue must not read as an assignment this Space made."""
        out = workitem_projection.project_workitem(
            self.adopted(), issues={NODE_ID: _mirror_row(
                assignees=[{"login": "ada", "avatar_url": None}],
            )},
        )
        self.assertIsNone(out["assignee"])
        self.assertEqual(out["assignees"], [])
        self.assertEqual(out["github_assignees"], ["ada"])

    def test_the_title_is_the_mirrors_and_the_labels_are_the_snapshots(self) -> None:
        """§5.3, and §6.2's reason for the asymmetry: the poll fetches a
        title and deliberately does not fetch labels, because a nested
        ``labels`` connection was measured to halve the project ceiling."""
        out = workitem_projection.project_workitem(
            self.adopted(), issues={NODE_ID: _mirror_row(title="renamed upstream")}
        )
        self.assertEqual(out["title"], "renamed upstream")
        self.assertEqual(out["labels"], ["infra"])

    def test_a_mirror_that_does_carry_labels_wins(self) -> None:
        """Nothing writes them today; if something ever does, it is fresher
        than a snapshot taken once at adoption."""
        out = workitem_projection.project_workitem(
            self.adopted(), issues={NODE_ID: _mirror_row(labels=["upstream"])}
        )
        self.assertEqual(out["labels"], ["upstream"])

    def test_body_is_unknown_for_an_adopted_item(self) -> None:
        """The plan says the body comes from "the mirror only" (§5.3) and
        the mirror does not carry one (§5.2 — the poll never selects it).
        ``None`` is the honest reading of that contradiction; the issue URL
        is where a body is read."""
        out = workitem_projection.project_workitem(
            self.adopted(), issues={NODE_ID: _mirror_row(body="ignored")}
        )
        self.assertIsNone(out["body"])

    def test_an_unknown_state_is_none_rather_than_a_neighbour(self) -> None:
        out = workitem_projection.project_workitem(
            self.adopted(),
            issues={NODE_ID: _mirror_row(state="draft", state_reason="DUPLICATE")},
        )
        self.assertIsNone(out["status"])
        self.assertIsNone(out["state_reason"])

    def test_a_local_item_answers_for_itself(self) -> None:
        local = {
            "id": "x", "title": "local", "body": "notes", "labels": [],
            "status": "closed", "state_reason": "completed",
            "source": {"kind": "local"}, "assignee": "ada",
        }
        out = workitem_projection.project_workitem(local, issues={NODE_ID: _mirror_row(
            assignees=[{"login": "octocat", "avatar_url": None}],
        )})
        self.assertEqual(out["status"], "closed")
        self.assertEqual(out["assignee"], "ada")
        self.assertEqual(out["assignees"], ["ada"], "one field for both kinds")
        self.assertEqual(out["github_assignees"], [],
                         "no issue has an opinion about a local item")
        self.assertEqual(out["body"], "notes")
        self.assertFalse(out["stale"], "nothing external owns a local item")

    def test_matching_is_on_node_id_not_on_repo_and_number(self) -> None:
        """A repository rename changes ``repo``/``number``'s URL and leaves
        the node id alone, so matching on the pair would strand every
        adopted item the day a repo moves."""
        renamed = _mirror_row(number=99)
        out = workitem_projection.project_workitem(
            self.adopted(), issues={NODE_ID: renamed}
        )
        self.assertFalse(out["stale"])
        self.assertEqual(
            workitem_projection.tracked_node_ids([self.adopted()]),
            {NODE_ID: "8f3a1c92-6b0e-4c11-9a3d-5e2f7c1d0a44"},
        )


class StaleProjectionTests(unittest.TestCase):
    """The row of §5.3 that is the whole task: the mirror has nothing."""

    RECORD = {
        "id": "8f3a1c92-6b0e-4c11-9a3d-5e2f7c1d0a44",
        "title": "Rate-limit the GitHub poller",
        "labels": ["infra"],
        "source": {"kind": "github", "github": dict(GITHUB_REF)},
        "links": {"todo_ids": [], "session_ids": []},
    }

    def test_an_absent_issue_renders_from_the_snapshot_and_is_flagged(self) -> None:
        for label, issues in (
            ("the poller has never run", {}),
            ("the issue was deleted", {"I_other": _mirror_row(node_id="I_other")}),
        ):
            with self.subTest(label):
                out = workitem_projection.project_workitem(self.RECORD, issues=issues)
                self.assertTrue(out["stale"])
                self.assertEqual(out["title"], "Rate-limit the GitHub poller")
                self.assertEqual(out["labels"], ["infra"])
                self.assertEqual(out["source"]["github"], GITHUB_REF)
                # Unknown, not defaulted: absent beats wrong (§5.3).
                self.assertIsNone(out["status"])
                self.assertIsNone(out["state_reason"])
                self.assertIsNone(out["body"])
                self.assertEqual(out["github_assignees"], [],
                                 "the mirror asserts nothing about who is on it")

    def test_a_stale_item_still_says_who_this_space_put_on_it(self) -> None:
        """``assignee`` is not GitHub's (§13, amendment 33), so the mirror
        being gone does not make it unknown. Blanking it here would lose a
        fact that is on disk and perfectly readable."""
        record = dict(self.RECORD, assignee="dwivedi-ai")
        out = workitem_projection.project_workitem(record, issues={})
        self.assertTrue(out["stale"])
        self.assertEqual(out["assignee"], "dwivedi-ai")
        self.assertEqual(out["assignees"], ["dwivedi-ai"])
        self.assertEqual(out["github_assignees"], [])

    def test_an_unusable_mirror_document_reads_as_no_issues(self) -> None:
        for document in (None, {}, {"issues": None}, {"issues": []}, "nonsense", 7):
            with self.subTest(repr(document)):
                self.assertEqual(workitem_projection.mirror_issues(document), {})


class ProjectionTotalityTests(unittest.TestCase):
    """It cannot raise. The wire models are built outside the route's
    ``try``/``except`` on purpose, so a projection that could throw would
    put the R1 defect back one layer lower and harder to see."""

    def test_no_input_makes_it_raise(self) -> None:
        broken = [
            None, 7, "a string", [], object(),
            {"source": "not a dict"},
            {"source": {"kind": "github"}},                       # no ref
            {"source": {"kind": "github", "github": "nope"}},
            {"source": {"kind": "github", "github": {"node_id": 5}}},
        ]
        for record in broken:
            for issues in (None, {}, {"x": "not a row"}, "nonsense"):
                with self.subTest(record=repr(record), issues=repr(issues)):
                    out = workitem_projection.project_workitem(record, issues=issues)
                    self.assertIsInstance(out, dict)
                    self.assertIn("stale", out)
                    self.assertIn("assignees", out)

    def test_a_list_projection_survives_a_poisoned_row(self) -> None:
        rows = [{"id": "a", "title": "fine", "source": {"kind": "local"}}, None, 3]
        out = workitem_projection.project_workitems(rows, issues={})
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0]["title"], "fine")


# ── The store transition (§13, amendment 8) ─────────────────────────────────


class _StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.xo = tmp / "xo-projects" / "demo" / ".xo"
        self.xo.mkdir(parents=True)
        self.path = self.xo / "workitems.json"
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(tmp / "xo-projects"),
            "QUIRQ_STATE_ROOT": str(tmp / "quirq"),
        })
        env.start()
        self.addCleanup(env.stop)

    def local(self, **kwargs) -> dict:
        kwargs.setdefault("runtime", "claude_code")
        kwargs.setdefault("title", "a local note")
        return workitems_store.create_workitem(self.path, **kwargs)

    def document(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def assertValidDocument(self, label: str) -> None:
        if Draft7Validator is None:  # pragma: no cover - dev dependency
            self.skipTest(_SKIP_JSONSCHEMA)
        validator = Draft7Validator(_schema())
        errors = sorted(validator.iter_errors(self.document()),
                        key=lambda e: list(e.path))
        if errors:
            detail = "\n".join(
                f"  at /{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                for e in errors
            )
            self.fail(f"{label} fails workitems.schema.json:\n{detail}")


class StoreAdoptionTests(_StoreCase):
    def test_adoption_snapshots_title_and_labels_and_stores_nothing_else(self) -> None:
        record, created = workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=dict(GITHUB_REF),
            title="Rate-limit the poller", labels=["infra", "perf"],
        )
        self.assertTrue(created)
        self.assertEqual(record["title"], "Rate-limit the poller")
        self.assertEqual(record["labels"], ["infra", "perf"])
        self.assertEqual(record["source"], {"kind": "github", "github": GITHUB_REF})
        for field in workitems_store.GITHUB_OWNED_FIELDS:
            self.assertNotIn(
                field, record,
                f"{field} is GitHub's for an adopted item; absent, not null",
            )
        self.assertValidDocument("a fresh adoption")

    def test_adopting_the_same_issue_twice_returns_the_same_workitem(self) -> None:
        """A ``POST`` a UI can double-fire. The check is inside the store's
        lock, because "look it up, then create it" across two calls is how
        one issue ends up with two workitems."""
        first, created_first = workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=dict(GITHUB_REF), title="one",
        )
        second, created_second = workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=dict(GITHUB_REF), title="two",
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["title"], "one", "the snapshot is not refreshed")
        self.assertEqual(len(self.document()["items"]), 1)

    def test_a_tombstoned_adoption_does_not_block_a_new_one(self) -> None:
        first, _ = workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=dict(GITHUB_REF), title="one",
        )
        workitems_store.delete_workitem(self.path, first["id"])
        second, created = workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=dict(GITHUB_REF), title="two",
        )
        self.assertTrue(created)
        self.assertNotEqual(first["id"], second["id"])

    def test_adopting_an_existing_local_item_drops_what_github_owns(self) -> None:
        local = self.local(
            body="my notes", status="closed", state_reason="completed",
            assignee="ada", todo_ids=["t1"],
        )
        record, created = workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=dict(GITHUB_REF),
            title="Rate-limit the poller", labels=["infra"],
            workitem_id=local["id"],
        )
        self.assertFalse(created, "the record was kept, not minted")
        self.assertEqual(record["id"], local["id"])
        self.assertEqual(record["created_at"], local["created_at"])
        self.assertEqual(record["links"]["todo_ids"], ["t1"], "history survives")
        for field in workitems_store.GITHUB_OWNED_FIELDS:
            self.assertNotIn(field, record)
        self.assertValidDocument("a local item that became adopted")

    def test_re_pointing_an_adopted_item_at_another_issue_is_refused(self) -> None:
        record, _ = workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=dict(GITHUB_REF), title="one",
        )
        other = dict(GITHUB_REF, number=43, node_id="I_other")
        with self.assertRaises(workitems_store.WorkitemsStoreError) as caught:
            workitems_store.adopt_workitem(
                self.path, runtime="claude_code", github=other, title="two",
                workitem_id=record["id"],
            )
        self.assertEqual(caught.exception.code, "already_adopted")

    def test_two_workitems_cannot_track_one_issue(self) -> None:
        workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=dict(GITHUB_REF), title="one",
        )
        local = self.local()
        with self.assertRaises(workitems_store.WorkitemsStoreError) as caught:
            workitems_store.adopt_workitem(
                self.path, runtime="claude_code", github=dict(GITHUB_REF),
                title="two", workitem_id=local["id"],
            )
        self.assertEqual(caught.exception.code, "already_adopted")

    def test_adopting_an_absent_or_tombstoned_workitem_is_not_found(self) -> None:
        local = self.local()
        workitems_store.delete_workitem(self.path, local["id"])
        for target in (local["id"], "no-such-id"):
            with self.subTest(target):
                with self.assertRaises(workitems_store.WorkitemsStoreError) as caught:
                    workitems_store.adopt_workitem(
                        self.path, runtime="claude_code", github=dict(GITHUB_REF),
                        title="t", workitem_id=target,
                    )
                self.assertEqual(caught.exception.code, "workitem_not_found")

    def test_a_new_adoption_needs_a_title(self) -> None:
        """The snapshot is not optional: without it a stale adopted item
        renders as ``repo#42``, which is not a work item anyone can act on."""
        with self.assertRaises(workitems_store.WorkitemsStoreError) as caught:
            workitems_store.adopt_workitem(
                self.path, runtime="claude_code", github=dict(GITHUB_REF),
            )
        self.assertEqual(caught.exception.code, "invalid_value")

    def test_a_malformed_reference_is_refused(self) -> None:
        for bad in ({"repo": REPO}, dict(GITHUB_REF, number=0),
                    dict(GITHUB_REF, url="http://insecure"),
                    dict(GITHUB_REF, repo="not a repo")):
            with self.subTest(repr(bad)):
                with self.assertRaises(workitems_store.WorkitemsStoreError) as caught:
                    workitems_store.adopt_workitem(
                        self.path, runtime="claude_code", github=bad, title="t",
                    )
                self.assertEqual(caught.exception.code, "invalid_source")

    def test_a_corrupt_document_is_refused_by_both_directions(self) -> None:
        """O-E survives the new entry points: the bytes may be the only copy."""
        corrupt = b"{not json"
        self.path.write_bytes(corrupt)
        with self.assertRaises(workitems_store.WorkitemsStoreError) as caught:
            workitems_store.adopt_workitem(
                self.path, runtime="claude_code", github=dict(GITHUB_REF), title="t",
            )
        self.assertEqual(caught.exception.code, "corrupt_document")
        with self.assertRaises(workitems_store.WorkitemsStoreError) as caught:
            workitems_store.unadopt_workitem(self.path, "any-id")
        self.assertEqual(caught.exception.code, "corrupt_document")
        self.assertEqual(self.path.read_bytes(), corrupt)


class StoreUnadoptionTests(_StoreCase):
    def adopted(self, **kwargs) -> dict:
        record, _ = workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=dict(GITHUB_REF),
            title="Rate-limit the poller", labels=["infra"], **kwargs,
        )
        return record

    def test_unadopting_materialises_the_fields_the_schema_requires(self) -> None:
        """The reason this is not ``update_workitem``: an adopted record
        carries no ``status`` and the schema *requires* one on a local
        record, so the drop and the fill have to be one write."""
        record = self.adopted()
        out = workitems_store.unadopt_workitem(
            self.path, record["id"], status="closed", state_reason="not_planned",
        )
        self.assertEqual(out["source"], {"kind": "local"})
        self.assertEqual(out["status"], "closed")
        self.assertEqual(out["state_reason"], "not_planned")
        self.assertIsNone(out["assignee"])
        self.assertIsNone(out["body"])
        self.assertEqual(out["title"], "Rate-limit the poller", "the snapshot stays")
        self.assertEqual(out["labels"], ["infra"])
        self.assertValidDocument("an un-adopted workitem")

    def test_status_defaults_to_open_when_the_mirror_said_nothing(self) -> None:
        record = self.adopted()
        out = workitems_store.unadopt_workitem(self.path, record["id"])
        self.assertEqual(out["status"], "open", "the least-committal value")
        self.assertValidDocument("an un-adopted workitem with no mirror")

    def test_the_materialised_assignee_is_available_to_a_caller(self) -> None:
        """The parameter is the materialisation contract, even though the
        route passes ``None``: a local item is self-assignable only, so it
        will not import a peer's login."""
        record = self.adopted()
        out = workitems_store.unadopt_workitem(
            self.path, record["id"], assignee="ada", body="notes",
        )
        self.assertEqual(out["assignee"], "ada")
        self.assertEqual(out["body"], "notes")
        self.assertValidDocument("an un-adopted workitem with an assignee")

    def test_unadopting_a_local_item_is_a_no_op(self) -> None:
        local = self.local()
        before = self.path.read_bytes()
        out = workitems_store.unadopt_workitem(self.path, local["id"])
        self.assertEqual(out["id"], local["id"])
        self.assertEqual(self.path.read_bytes(), before, "nothing was rewritten")

    def test_a_round_trip_leaves_a_record_the_schema_still_accepts(self) -> None:
        local = self.local(body="notes", assignee="ada")
        adopted, _ = workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=dict(GITHUB_REF),
            title="issue title", workitem_id=local["id"],
        )
        self.assertValidDocument("mid-round-trip")
        back = workitems_store.unadopt_workitem(self.path, adopted["id"])
        self.assertValidDocument("after the round trip")
        self.assertEqual(list(back), list(self.local()), "canonical key order")

    def test_an_absent_or_tombstoned_workitem_is_not_found(self) -> None:
        record = self.adopted()
        workitems_store.delete_workitem(self.path, record["id"])
        for target in (record["id"], "no-such-id"):
            with self.subTest(target):
                with self.assertRaises(workitems_store.WorkitemsStoreError) as caught:
                    workitems_store.unadopt_workitem(self.path, target)
                self.assertEqual(caught.exception.code, "workitem_not_found")


# ── The routes ──────────────────────────────────────────────────────────────


class _RoutedCase(unittest.TestCase):
    """A scaffolded, pid-bearing project with a github.com remote, both
    roots redirected, and a client on the real router."""

    PROJECT = "demo"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.root = tmp / "xo-projects"
        self.xo = self.root / self.PROJECT / ".xo"
        self.xo.mkdir(parents=True)
        (self.xo / "project.json").write_text(
            json.dumps({
                "schema": 2,
                "pid": PID,
                "name": self.PROJECT,
                "owner_user_id": "local",
                "created_at": "2026-01-01T00:00:00Z",
                "git": {"remote_url": f"https://github.com/{REPO}.git"},
            }),
            encoding="utf-8",
        )
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.root),
            "QUIRQ_STATE_ROOT": str(tmp / "quirq"),
        })
        env.start()
        self.addCleanup(env.stop)

        # The poller's interest map and budget are in-process globals; a
        # test that left either set would change how the next one behaves.
        github_poller.reset_state()
        self.addCleanup(github_poller.reset_state)

        # No test here may reach the network. The stored-token path is the
        # first thing "assign to me" tries, so it is closed by default and
        # opened deliberately by the cases that exercise it.
        token = patch(
            "services.cowork_agent.connectors.github_connector.get_github_token",
            return_value=None,
        )
        token.start()
        self.addCleanup(token.stop)

        app = FastAPI()
        from routers.cowork_agent.bff.visualizer import router

        app.include_router(router)
        self.client = TestClient(app)
        self.base = f"/api/xo-projects/{self.PROJECT}"

    # ── helpers ──────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self.xo / "workitems.json"

    def stored(self, workitem_id: str) -> dict:
        return json.loads(self.path.read_text("utf-8"))["items"][workitem_id]

    def write_mirror(self, *rows: dict, error: dict | None = None,
                     fetched_at: str | None = "2026-09-08T12:00:00Z") -> Path:
        path = github_mirror.mirror_path(self.PROJECT, create=True)
        assert path is not None
        path.write_text(json.dumps({
            "$schema": "xo/github-issues.schema.json",
            "schema": 1,
            "repo": REPO,
            "fetched_at": fetched_at,
            "since": None,
            "rate": None,
            "error": error,
            "issues": {row["node_id"]: row for row in rows},
        }), encoding="utf-8")
        return path

    def create_local(self, **body) -> dict:
        payload = {"runtime": "claude_code", "title": "a local note"}
        payload.update(body)
        res = self.client.post(f"{self.base}/workitems", json=payload)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    def adopt(self, number: int = 42, **body):
        payload = {"runtime": "claude_code"}
        payload.update(body)
        return self.client.post(
            f"{self.base}/github/issues/{number}/adopt", json=payload
        )


class IssuesRouteTests(_RoutedCase):
    def test_the_mirror_is_served_with_an_untracked_count(self) -> None:
        self.write_mirror(
            _mirror_row(),
            _mirror_row(node_id="I_two", number=43, title="second",
                        updated_at="2026-09-08T13:00:00Z"),
            _mirror_row(node_id="I_closed", number=7, state="closed",
                        updated_at="2026-09-01T00:00:00Z"),
        )
        res = self.client.get(f"{self.base}/github/issues")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["repo"], REPO)
        self.assertEqual(body["fetched_at"], "2026-09-08T12:00:00Z")
        self.assertEqual([row["number"] for row in body["issues"]], [43, 42, 7],
                         "newest updated first")
        self.assertEqual(body["untracked"], 2, "open and untracked only")
        self.assertEqual(body["tracked"], 0)
        self.assertIsNone(body["issues"][0]["labels"],
                          "absent, never [] — the poll does not fetch labels")

    def test_an_adopted_issue_is_marked_and_leaves_the_untracked_count(self) -> None:
        self.write_mirror(_mirror_row())
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            adopted = self.adopt().json()
        body = self.client.get(f"{self.base}/github/issues").json()
        row = body["issues"][0]
        self.assertTrue(row["adopted"])
        self.assertEqual(row["workitem_id"], adopted["id"])
        self.assertEqual(body["untracked"], 0)
        self.assertEqual(body["tracked"], 1)

    def test_no_mirror_is_an_empty_answer_not_an_error(self) -> None:
        res = self.client.get(f"{self.base}/github/issues")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["issues"], [])
        self.assertIsNone(body["fetched_at"], "no poll has succeeded yet")
        self.assertEqual(body["repo"], REPO, "read from the project's own remote")
        self.assertIsNone(body["error"])

    def test_the_last_failure_is_surfaced_for_its_affordance(self) -> None:
        """"Connect GitHub" and "the network is down" need different
        buttons, which is why the mirror's error is structured."""
        self.write_mirror(error={
            "kind": "not_authenticated", "message": "gh auth login",
            "at": "2026-09-08T12:05:00Z",
        }, fetched_at=None)
        body = self.client.get(f"{self.base}/github/issues").json()
        self.assertEqual(body["error"]["kind"], "not_authenticated")
        self.assertEqual(body["error"]["at"], "2026-09-08T12:05:00Z")

    def test_it_is_the_caller_of_the_lazy_polling_hook(self) -> None:
        """D9's ``note_interest`` had no caller anywhere in the system —
        there is no viewing signal — and this is the closest thing to one:
        browsing a project's issues is what someone does when they are
        looking at it, and it is the state a project with no adopted items
        would otherwise never be polled in."""
        self.assertEqual(github_poller.interested_projects(), set())
        self.client.get(f"{self.base}/github/issues")
        self.assertIn(self.PROJECT, github_poller.interested_projects())

    def test_an_unreadable_workitems_file_still_serves_the_issues(self) -> None:
        """The 409 that document deserves is answered on the workitems
        surface. Here it would cost the browse view over one derived
        field, which degrades to "nothing is tracked"."""
        self.write_mirror(_mirror_row())
        self.path.write_bytes(b"{not json")
        with self.assertLogs("routers.cowork_agent.bff.visualizer", "WARNING"):
            res = self.client.get(f"{self.base}/github/issues")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(len(res.json()["issues"]), 1)
        self.assertFalse(res.json()["issues"][0]["adopted"])

    def test_an_unknown_project_is_404(self) -> None:
        res = self.client.get("/api/xo-projects/nope/github/issues")
        self.assertEqual(res.status_code, 404, res.text)
        self.assertEqual(res.json()["detail"]["code"], "project_not_found")

    def test_a_poisoned_mirror_still_renders(self) -> None:
        """The mirror is rebuilt by a background loop and read on a
        request. One row this revision cannot read must not cost the
        caller the other eighty — and a sort key that could be a string
        beside an int is how a route whose job is to keep rendering
        raises ``TypeError`` instead."""
        path = github_mirror.mirror_path(self.PROJECT, create=True)
        path.write_text(json.dumps({
            "schema": 1, "repo": REPO, "fetched_at": "2026-09-08T12:00:00Z",
            "issues": {
                "A": {"node_id": "A", "number": "not-an-int", "title": 5,
                      "state": "weird", "assignees": 7, "labels": "nope",
                      "url": None, "updated_at": 3},
                "B": "not even a row",
                "C": _mirror_row(node_id="C", number=8),
            },
        }), encoding="utf-8")
        with self.assertLogs("routers.cowork_agent.bff.visualizer", "WARNING"):
            res = self.client.get(f"{self.base}/github/issues")
        self.assertEqual(res.status_code, 200, res.text)
        rows = {row["node_id"]: row for row in res.json()["issues"]}
        self.assertEqual(set(rows), {"A", "C"}, "the non-row is dropped")
        self.assertIsNone(rows["A"]["state"], "unknown, never invented")
        self.assertEqual(rows["A"]["assignees"], [])
        self.assertEqual(rows["C"]["number"], 8)

    def test_a_poisoned_mirror_does_not_take_down_the_workitems_list(self) -> None:
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            self.adopt()
        path = github_mirror.mirror_path(self.PROJECT, create=True)
        path.write_text('{"schema": 1, "issues": "not a map"}', encoding="utf-8")
        res = self.client.get(f"{self.base}/workitems")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["workitems"][0]["stale"])


class AdoptRouteTests(_RoutedCase):
    def test_adoption_snapshots_the_title_and_the_labels_it_fetched(self) -> None:
        """The one-off fetch is what "labels are fetched lazily at
        adoption" means: the poll leaves them out because a nested
        ``labels`` connection halves the project ceiling (§6.2)."""
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            res = self.adopt()
        self.assertEqual(res.status_code, 201, res.text)
        item = res.json()
        self.assertEqual(item["title"], "Rate-limit the poller")
        self.assertEqual(item["labels"], ["infra", "perf"])
        self.assertEqual(item["source"]["kind"], "github")
        self.assertEqual(item["source"]["github"], GITHUB_REF)
        stored = self.stored(item["id"])
        for field in ("status", "state_reason", "body"):
            self.assertNotIn(field, stored, f"{field} is GitHub's, not stored")
        self.assertIsNone(stored["assignee"],
                          "assignee is ours (§13, amendment 33) — stored, and "
                          "null until somebody assigns it")

    def test_a_second_adopt_of_one_issue_is_200_and_the_same_record(self) -> None:
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            first = self.adopt()
            second = self.adopt()
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["id"], second.json()["id"])

    def test_an_existing_local_workitem_can_become_the_record(self) -> None:
        local = self.create_local(body="my notes", assignee=None, todo_ids=["t1"])
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            res = self.adopt(workitem_id=local["id"])
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["id"], local["id"])
        self.assertEqual(res.json()["links"]["todo_ids"], ["t1"])
        self.assertNotIn("body", self.stored(local["id"]))

    def test_it_falls_back_to_the_mirror_when_github_is_unreachable(self) -> None:
        """Adoption without labels beats no adoption: the reference is the
        part that matters, and it is already in the mirror."""
        self.write_mirror(_mirror_row(title="from the mirror"))
        with patch.object(github_issue_actions, "fetch_issue",
                          _fetch_failing("network", "no route to host")):
            res = self.adopt()
        self.assertEqual(res.status_code, 201, res.text)
        self.assertEqual(res.json()["title"], "from the mirror")
        self.assertEqual(res.json()["labels"], [], "nothing fetched them")

    def test_with_neither_github_nor_a_mirror_it_says_which(self) -> None:
        for kind, status, code in (
            ("network", 502, "github_unavailable"),
            ("not_authenticated", 503, "github_not_connected"),
            ("not_found", 404, "issue_not_found"),
            ("no_cli", 503, "github_unavailable"),
            ("forbidden", 403, "github_forbidden"),
        ):
            with self.subTest(kind):
                with patch.object(github_issue_actions, "fetch_issue",
                                  _fetch_failing(kind)):
                    res = self.adopt()
                self.assertEqual(res.status_code, status, res.text)
                self.assertEqual(res.json()["detail"]["code"], code)
        self.assertFalse(self.path.exists(), "nothing was written")

    def test_a_project_with_no_github_remote_is_a_400(self) -> None:
        (self.xo / "project.json").write_text(
            json.dumps({"schema": 2, "pid": PID, "name": self.PROJECT}),
            encoding="utf-8",
        )
        async def _explode(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("spent a point on a project with no remote")

        with patch.object(github_issue_actions, "fetch_issue", _explode):
            res = self.adopt()
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(res.json()["detail"]["code"], "not_a_github_project")

    def test_a_corrupt_workitems_document_is_a_409_with_no_path(self) -> None:
        self.path.write_bytes(b"{not json")
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            res = self.adopt()
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "corrupt_document")
        self.assertNotIn(str(self.path), res.json()["detail"]["message"])

    def test_an_invalid_runtime_is_a_400(self) -> None:
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            res = self.adopt(runtime="bad runtime")
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(res.json()["detail"]["code"], "invalid_runtime")


class AdoptedProjectionRouteTests(_RoutedCase):
    """W7's acceptance criterion, over HTTP: from the mirror when it is
    there, from the file when it is not, **never** a 404."""

    def adopted(self) -> dict:
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            res = self.adopt()
        self.assertIn(res.status_code, (200, 201), res.text)
        return res.json()

    def test_an_adopted_item_renders_from_the_mirror(self) -> None:
        item = self.adopted()
        self.write_mirror(_mirror_row(
            title="renamed on GitHub", state="closed", state_reason="completed",
            assignees=[{"login": "ada", "avatar_url": None}],
        ))
        for url in (f"{self.base}/workitems", f"{self.base}/workitems/{item['id']}"):
            with self.subTest(url):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 200, res.text)
                body = res.json()
                row = body["workitems"][0] if "workitems" in body else body
                self.assertEqual(row["title"], "renamed on GitHub")
                self.assertEqual(row["status"], "closed")
                self.assertEqual(row["state_reason"], "completed")
                self.assertIsNone(row["assignee"],
                                  "nobody assigned it *here*")
                self.assertFalse(row["assigned"])
                self.assertEqual(row["assignees"], [])
                self.assertEqual(row["github_assignees"], ["ada"],
                                 "who GitHub has on the issue, kept separate")
                self.assertEqual(row["origin"], "github")
                self.assertFalse(row["stale"])
                self.assertEqual(row["labels"], ["infra", "perf"],
                                 "the adoption snapshot, never refreshed")

    def test_with_the_mirror_deleted_it_still_renders_and_never_404s(self) -> None:
        """The row of §10 this task exists for: *with the mirror deleted it
        still renders from the file, flagged stale, never 404*."""
        item = self.adopted()
        path = self.write_mirror(_mirror_row())
        self.assertEqual(
            self.client.get(f"{self.base}/workitems/{item['id']}").json()["stale"],
            False,
        )
        path.unlink()

        res = self.client.get(f"{self.base}/workitems/{item['id']}")
        self.assertEqual(res.status_code, 200, res.text)
        row = res.json()
        self.assertTrue(row["stale"])
        self.assertEqual(row["title"], "Rate-limit the poller", "the snapshot")
        self.assertEqual(row["labels"], ["infra", "perf"])
        self.assertEqual(row["source"]["github"], GITHUB_REF)
        self.assertIsNone(row["status"], "unknown, not defaulted")
        self.assertIsNone(row["assignee"])
        self.assertEqual(row["assignees"], [])
        self.assertEqual(row["github_assignees"], [])
        self.assertEqual(row["origin"], "github", "still a GitHub workitem")

        listed = self.client.get(f"{self.base}/workitems")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertTrue(listed.json()["workitems"][0]["stale"])

    def test_an_issue_missing_from_a_present_mirror_is_stale_too(self) -> None:
        """Deleted or transferred upstream — the mirror is healthy and the
        issue is simply not in it."""
        item = self.adopted()
        self.write_mirror(_mirror_row(node_id="I_someone_else", number=99))
        res = self.client.get(f"{self.base}/workitems/{item['id']}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["stale"])
        self.assertIsNone(res.json()["status"])

    def test_a_local_item_is_never_stale_and_keeps_its_own_fields(self) -> None:
        local = self.create_local(assignee="ada", body="notes")
        self.write_mirror(_mirror_row())
        row = self.client.get(f"{self.base}/workitems/{local['id']}").json()
        self.assertFalse(row["stale"])
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["assignee"], "ada")
        self.assertTrue(row["assigned"])
        self.assertEqual(row["assignees"], ["ada"])
        self.assertEqual(row["github_assignees"], [])
        self.assertEqual(row["origin"], "space",
                         "not from GitHub, so it is from this Space")
        self.assertEqual(row["body"], "notes")


class UnadoptRouteTests(_RoutedCase):
    def adopted(self) -> dict:
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            return self.adopt().json()

    def test_unadopting_keeps_the_workitem_and_materialises_the_state(self) -> None:
        item = self.adopted()
        self.write_mirror(_mirror_row(state="closed", state_reason="not_planned",
                                      assignees=[{"login": "ada"}]))
        res = self.client.delete(f"{self.base}/workitems/{item['id']}/adoption")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["id"], item["id"], "the workitem was kept")
        self.assertEqual(body["source"], {"kind": "local", "github": None})
        self.assertEqual(body["status"], "closed", "as the projection was serving it")
        self.assertEqual(body["state_reason"], "not_planned")
        self.assertEqual(body["title"], "Rate-limit the poller")
        self.assertEqual(body["labels"], ["infra", "perf"])
        self.assertIsNone(body["assignee"],
                          "a local item is self-assignable only; a peer's "
                          "login would be an assignment nothing coordinates")
        self.assertEqual(self.stored(item["id"])["status"], "closed")

    def test_with_no_mirror_the_status_falls_back_to_open(self) -> None:
        item = self.adopted()
        res = self.client.delete(f"{self.base}/workitems/{item['id']}/adoption")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["status"], "open")

    def test_it_is_idempotent_on_an_already_local_item(self) -> None:
        local = self.create_local()
        res = self.client.delete(f"{self.base}/workitems/{local['id']}/adoption")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["id"], local["id"])
        self.assertEqual(res.json()["status"], "open")

    def test_an_unknown_workitem_is_still_a_404(self) -> None:
        res = self.client.delete(f"{self.base}/workitems/nope/adoption")
        self.assertEqual(res.status_code, 404, res.text)
        self.assertEqual(res.json()["detail"]["code"], "workitem_not_found")

    def test_the_item_can_be_re_adopted_afterwards(self) -> None:
        item = self.adopted()
        self.client.delete(f"{self.base}/workitems/{item['id']}/adoption")
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            again = self.adopt()
        self.assertEqual(again.status_code, 201, again.text)
        self.assertNotEqual(again.json()["id"], item["id"],
                            "a fresh record; the old one is a local note now")


class AssignmentTests(_RoutedCase):
    """One path for both kinds, and it writes ``.xo/workitems.json``.

    D1 made assignment a GitHub assignee and W8 implemented two halves —
    a ``PATCH`` against the issue for an adopted item, a self-only local
    write for the other. That decision was reversed (§13, amendment 33).
    There is one path now: the assignee is a local annotation, stored for
    every workitem, and nothing on this route reaches GitHub.
    """

    def adopted(self) -> dict:
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            return self.adopt().json()

    def assign(self, workitem_id: str, assignee):
        return self.client.put(
            f"{self.base}/workitems/{workitem_id}/assignee",
            json={"assignee": assignee},
        )

    # ── the local half ───────────────────────────────────────────────

    def test_assigning_a_peer_is_accepted_and_stored(self) -> None:
        """The reversal, in one assertion. This was ``400
        local_assignee_only``: a local workitem could never reach a peer,
        because reaching a peer *was* a GitHub assignee. With no GitHub
        write left, the refusal guards nothing — so any identity is
        accepted, and what it means is documented rather than enforced."""
        local = self.create_local()
        res = self.assign(local["id"], "some-peer")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["assignee"], "some-peer")
        self.assertEqual(self.stored(local["id"])["assignee"], "some-peer")

    def test_assigning_to_me_resolves_to_this_space(self) -> None:
        local = self.create_local()
        with patch.dict(os.environ, {"CODER_WORKSPACE_OWNER_NAME": "ankitdwivedi"}):
            res = self.assign(local["id"], "me")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["kind"], "local")
        self.assertEqual(body["assignee"], "ankitdwivedi")
        self.assertEqual(body["assignees"], ["ankitdwivedi"])
        self.assertFalse(body["pending"], "the file is the record and it is written")
        self.assertEqual(self.stored(local["id"])["assignee"], "ankitdwivedi")

    def test_the_space_s_own_name_is_accepted_spelled_out(self) -> None:
        local = self.create_local()
        with patch.dict(os.environ, {"CODER_WORKSPACE_OWNER_NAME": "ankitdwivedi"}):
            res = self.assign(local["id"], "ankitdwivedi")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["assignee"], "ankitdwivedi")

    def test_a_leading_at_is_not_part_of_the_name(self) -> None:
        """``@octocat`` and ``octocat`` are one person. Stored with the
        ``@`` the name would fail the store's charset and answer 400 for a
        spelling the rollup filter accepts."""
        local = self.create_local()
        res = self.assign(local["id"], "@octocat")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["assignee"], "octocat")

    def test_null_clears_the_assignee(self) -> None:
        local = self.create_local(assignee="local")
        res = self.assign(local["id"], None)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsNone(res.json()["assignee"])
        self.assertEqual(res.json()["assignees"], [])
        self.assertIsNone(self.stored(local["id"])["assignee"])

    def test_an_unknown_workitem_is_a_404(self) -> None:
        res = self.assign("nope", None)
        self.assertEqual(res.status_code, 404, res.text)
        self.assertEqual(res.json()["detail"]["code"], "workitem_not_found")

    def test_the_body_must_carry_the_key(self) -> None:
        """A ``PUT`` of one value has no meaning for an omitted key, so it
        is required-but-nullable rather than optional."""
        local = self.create_local()
        res = self.client.put(
            f"{self.base}/workitems/{local['id']}/assignee", json={}
        )
        self.assertEqual(res.status_code, 422, res.text)

    def test_an_unstorable_name_is_a_400_not_a_silent_write(self) -> None:
        local = self.create_local()
        res = self.assign(local["id"], "not a login")
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(res.json()["detail"]["code"], "invalid_assignee")

    # ── the adopted half, which is now the same half ─────────────────

    def test_an_adopted_item_is_assigned_locally_and_nothing_is_pending(self) -> None:
        item = self.adopted()
        self.write_mirror(_mirror_row())
        mirror_before = github_mirror.mirror_path(self.PROJECT).read_bytes()

        res = self.assign(item["id"], "ada")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["kind"], "github", "the workitem's kind, as stored")
        self.assertEqual(body["assignee"], "ada")
        self.assertFalse(body["pending"],
                         "nothing is outstanding: the write already landed")
        self.assertEqual(
            self.stored(item["id"])["assignee"], "ada",
            "the synced tier is where an assignment lives now",
        )
        self.assertEqual(
            github_mirror.mirror_path(self.PROJECT).read_bytes(), mirror_before,
            "the poller is still the mirror's single writer",
        )

    def test_an_adopted_items_assignee_is_served_beside_githubs(self) -> None:
        item = self.adopted()
        self.write_mirror(_mirror_row(
            assignees=[{"login": "octocat", "avatar_url": None}],
        ))
        self.assign(item["id"], "ada")
        row = self.client.get(f"{self.base}/workitems/{item['id']}").json()
        self.assertEqual(row["assignee"], "ada")
        self.assertTrue(row["assigned"])
        self.assertEqual(row["github_assignees"], ["octocat"])

    def test_me_needs_no_github_login_for_an_adopted_item(self) -> None:
        """It used to resolve through ``gh api user`` and answer ``503
        github_not_connected`` when it could not. There is nothing to
        route now, so ``me`` is this Space's own name for both kinds and
        an unauthenticated machine assigns perfectly well."""
        item = self.adopted()
        with patch.dict(os.environ, {"CODER_WORKSPACE_OWNER_NAME": "ankitdwivedi"}), \
                patch.object(github_issue_actions, "authenticated_login", _no_login):
            res = self.assign(item["id"], "me")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["assignee"], "ankitdwivedi")

    def test_a_paused_poller_does_not_stop_an_assignment(self) -> None:
        """The budget gate went with the GitHub call. A rate-limited or
        unauthenticated machine cannot poll; it can still say who owes
        what, because that is a local write."""
        item = self.adopted()
        with patch.object(github_poller, "budget_snapshot", return_value={
            "spent_last_hour": 0, "remaining": 0, "limit": 5000,
            "reset_at": None, "paused": True, "pause_reason": "not_authenticated",
        }):
            res = self.assign(item["id"], "ada")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["assignee"], "ada")

    def test_an_unusable_issue_reference_no_longer_blocks_assignment(self) -> None:
        """It was a 409: the record claimed to mirror an issue but could
        not say which, and guessing a repository would have assigned work
        on somebody else's. No request is made now, so there is nothing to
        guess and no reason to refuse a local annotation."""
        item = self.adopted()
        doc = json.loads(self.path.read_text("utf-8"))
        doc["items"][item["id"]]["source"]["github"] = {"node_id": NODE_ID}
        self.path.write_text(json.dumps(doc), encoding="utf-8")

        res = self.assign(item["id"], "ada")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self.stored(item["id"])["assignee"], "ada")

    def test_a_corrupt_document_is_still_a_409(self) -> None:
        local = self.create_local()
        self.path.write_bytes(b"{not json")
        res = self.assign(local["id"], "ada")
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "corrupt_document")
        self.assertNotIn(str(self.path), res.json()["detail"]["message"])


class NoGithubWriteTests(_RoutedCase):
    """**No assignment can reach GitHub.** The point of the reversal, and
    the one property worth proving at the seam rather than at the module
    boundary every other case stubs.

    ``set_assignees`` is deleted, so there is no function left to patch
    and assert was not called — an absence is not evidence. Instead this
    blows up ``_run_gh``, which is how *every* call in this system reaches
    the ``gh`` binary, and then assigns. Anything that tried to talk to
    GitHub — the deleted write, a resurrected one, a ``me`` lookup, a
    budget probe — raises through the route and fails the test.
    """

    def setUp(self) -> None:
        super().setUp()

        async def _explode(*args, **kwargs):
            raise AssertionError(
                "assignment reached the `gh` seam; GitHub is read-only "
                "(workitems-plan §13, amendment 33)"
            )

        for module in (github_issues, github_issue_actions):
            patcher = patch.object(module, "_run_gh", _explode)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_connector_no_longer_has_a_write_at_all(self) -> None:
        """Deleted, not left unused: a function that still exists is a
        function something can be wired back to by accident."""
        self.assertFalse(hasattr(github_issue_actions, "set_assignees"))
        self.assertFalse(hasattr(github_issue_actions, "AssignResult"))
        for name in ("fetch_issue", "authenticated_login"):
            self.assertTrue(hasattr(github_issue_actions, name),
                            f"{name} is a read and is still needed")

    def test_neither_kind_of_assignment_touches_the_gh_seam(self) -> None:
        local = self.create_local()
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            adopted = self.adopt().json()

        for label, workitem_id in (("local", local["id"]),
                                   ("adopted", adopted["id"])):
            for value in ("a-peer", "me", None):
                with self.subTest(kind=label, assignee=value):
                    res = self.client.put(
                        f"{self.base}/workitems/{workitem_id}/assignee",
                        json={"assignee": value},
                    )
                    self.assertEqual(res.status_code, 200, res.text)

    def test_the_assignment_landed_in_the_synced_document(self) -> None:
        """Not merely "it did not call GitHub" — it wrote where it says
        it writes, for the adopted item as much as the local one."""
        with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
            adopted = self.adopt().json()
        self.client.put(
            f"{self.base}/workitems/{adopted['id']}/assignee",
            json={"assignee": "a-peer"},
        )
        stored = json.loads(self.path.read_text("utf-8"))["items"][adopted["id"]]
        self.assertEqual(stored["assignee"], "a-peer")


class BudgetSharingTests(_RoutedCase):
    """Interactive calls share the poller's global budget (§6.3). They do
    not stop at its reserve — the reserve exists *for* them — but they do
    stand down when the poller has, because a pause means a state that is
    true of the whole machine."""

    def test_a_paused_poller_refuses_the_interactive_calls(self) -> None:
        item_client = self.client
        with patch.object(github_poller, "budget_snapshot", return_value={
            "spent_last_hour": 0, "remaining": 4000, "limit": 5000,
            "reset_at": None, "paused": True,
            "pause_reason": "rate limited on dwivedi-ai/xo-cowork-api",
        }):
            async def _explode(*args, **kwargs):  # pragma: no cover
                raise AssertionError("spent budget while the poller was paused")

            with patch.object(github_issue_actions, "fetch_issue", _explode):
                res = item_client.post(
                    f"{self.base}/github/issues/42/adopt",
                    json={"runtime": "claude_code"},
                )
        self.assertEqual(res.status_code, 503, res.text)
        self.assertEqual(res.json()["detail"]["code"], "github_rate_limited")
        self.assertIn("rate limited", res.json()["detail"]["message"])

    def test_a_spent_budget_refuses_too(self) -> None:
        with patch.object(github_poller, "budget_snapshot", return_value={
            "spent_last_hour": 5000, "remaining": 0, "limit": 5000,
            "reset_at": "2026-09-08T13:00:00Z", "paused": False,
            "pause_reason": "",
        }):
            res = self.adopt()
        self.assertEqual(res.status_code, 503, res.text)
        self.assertEqual(res.json()["detail"]["code"], "github_rate_limited")
        self.assertIn("2026-09-08T13:00:00Z", res.json()["detail"]["message"])

    def test_the_reserve_itself_is_spendable(self) -> None:
        """``BUDGET_RESERVE_POINTS`` is headroom the poller leaves *for*
        these calls; refusing at it would make the reserve pointless."""
        self.assertLess(0, github_poller.BUDGET_RESERVE_POINTS)
        with patch.object(github_poller, "budget_snapshot", return_value={
            "spent_last_hour": 4800,
            "remaining": github_poller.BUDGET_RESERVE_POINTS,
            "limit": 5000, "reset_at": None, "paused": False, "pause_reason": "",
        }):
            with patch.object(github_issue_actions, "fetch_issue", _fetch_ok()):
                res = self.adopt()
        self.assertEqual(res.status_code, 201, res.text)


# ── The `gh` calls themselves, against a fake binary ────────────────────────


class GhInvocationTests(unittest.TestCase):
    """The connector, driven end to end against a fake ``gh`` on disk.

    Every other case in this file stubs the connector at the module
    boundary, which is right for testing the routes and wrong for testing
    the connector: it would leave the argv, the request body and the
    response parsing — the parts that actually talk to GitHub — with no
    coverage at all, offline *or* online. So this class writes a shell
    script called ``gh``, hands its path in as ``gh_bin``, and asserts on
    what the real code asked it for.

    Still no network and no real ``gh``.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.argv_log = self.dir / "argv.txt"
        self.body_log = self.dir / "body.json"

    def fake_gh(self, stdout: str, *, exit_code: int = 0,
                capture_input: bool = False) -> str:
        """A ``gh`` that records its argv and prints ``stdout``."""
        script = self.dir / "gh"
        capture = (
            'for a in "$@"; do\n'
            '  if [ "$prev" = "--input" ]; then cp "$a" "%s"; fi\n'
            '  prev="$a"\n'
            'done\n' % self.body_log
        ) if capture_input else ""
        script.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$@" > ' + str(self.argv_log) + "\n"
            + capture
            + "cat <<'EOF'\n" + stdout + "\nEOF\n"
            + f"exit {exit_code}\n"
        )
        script.chmod(0o755)
        return str(script)

    def argv(self) -> list[str]:
        return self.argv_log.read_text("utf-8").splitlines()

    # ── the pinned query ─────────────────────────────────────────────

    def test_the_query_shape_is_pinned_at_two_connections(self) -> None:
        """The cost contract, checkable without a network call — the same
        guard ``tests/test_github_issues.py`` puts on the poll. Measured
        against ``cjpais/Handy`` on 2026-09-08 at **1 point**, labels
        included: the doubling §6.2 warns about is per page of 100
        issues, not per issue."""
        self.assertEqual(
            query_connections(github_issue_actions.ISSUE_QUERY),
            (("assignees", "5"), ("labels", "20")),
        )

    # ── fetch_issue ──────────────────────────────────────────────────

    GRAPHQL_OK = json.dumps({
        "data": {
            "rateLimit": {"limit": 5000, "cost": 1, "remaining": 4999,
                          "resetAt": "2026-09-08T13:00:00Z"},
            "repository": {"issue": {
                "id": NODE_ID, "number": 42, "title": "Rate-limit the poller",
                "url": f"https://github.com/{REPO}/issues/42",
                "state": "OPEN", "stateReason": None,
                "updatedAt": "2026-09-08T12:00:00Z",
                "labels": {"nodes": [{"name": "infra"}, {"name": "perf"}]},
                "assignees": {"nodes": [{"login": "ada", "avatarUrl": "https://a"}]},
            }},
        }
    })

    def test_fetch_issue_asks_for_the_labels_and_parses_them(self) -> None:
        gh = self.fake_gh(self.GRAPHQL_OK)
        result = asyncio.run(
            github_issue_actions.fetch_issue(REPO, 42, gh_bin=gh)
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.issue["labels"], ["infra", "perf"])
        self.assertEqual(result.issue["node_id"], NODE_ID)
        self.assertEqual(result.issue["state"], "open")
        self.assertEqual(result.issue["assignees"],
                         [{"login": "ada", "avatar_url": "https://a"}])
        self.assertEqual(result.rate.cost, 1, "one point, as measured")
        argv = self.argv()
        self.assertEqual(argv[:3], ["api", "graphql", "-f"])
        self.assertIn("owner=dwivedi-ai", argv)
        self.assertIn("name=xo-cowork-api", argv)
        self.assertIn("number=42", argv)

    def test_a_graphql_not_found_keeps_its_kind(self) -> None:
        gh = self.fake_gh(json.dumps({
            "data": {"repository": {"issue": None}},
            "errors": [{"type": "NOT_FOUND", "message": "no such issue"}],
        }))
        result = asyncio.run(github_issue_actions.fetch_issue(REPO, 9, gh_bin=gh))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "not_found")

    def test_a_missing_binary_is_a_state_not_an_exception(self) -> None:
        result = asyncio.run(
            github_issue_actions.fetch_issue(
                REPO, 42, gh_bin=str(self.dir / "definitely-not-here"),
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "no_cli")

    # ── set_assignees is gone ────────────────────────────────────────
    #
    # There were four cases here driving the ``PATCH …/issues/{n}`` against
    # a fake ``gh``: the assignees array that replaces the set, the empty
    # array that clears it, the temp-file request body being cleaned up, and
    # a rejected credential keeping its error kind. All four tested a write
    # to GitHub, and there is no longer a write to GitHub — §13 amendment 33
    # deleted ``set_assignees`` rather than leaving it unused. What replaced
    # them is ``NoGithubWriteTests``, which asserts the *absence* at the
    # subprocess seam instead of the presence at the argv.

    # ── authenticated_login ──────────────────────────────────────────

    def test_the_spaces_own_login_is_read_from_gh(self) -> None:
        gh = self.fake_gh(json.dumps({"login": "dwivedi-ai", "id": 1}))
        found = asyncio.run(github_issue_actions.authenticated_login(gh_bin=gh))
        self.assertTrue(found.ok)
        self.assertEqual(found.login, "dwivedi-ai")
        self.assertEqual(self.argv(), ["api", "user"])

    def test_an_unauthenticated_gh_answers_a_kind(self) -> None:
        gh = self.fake_gh(
            json.dumps({"message": "Bad credentials", "status": "401"}),
            exit_code=1,
        )
        found = asyncio.run(github_issue_actions.authenticated_login(gh_bin=gh))
        self.assertFalse(found.ok)
        self.assertIsNone(found.login)
        self.assertEqual(found.error_kind, "not_authenticated")


class ErrorTableTests(unittest.TestCase):
    def test_every_gh_error_kind_has_an_http_answer(self) -> None:
        """The vocabulary is closed (``ERROR_KINDS``) and the schema
        already asserts the mirror agrees with it. This is the third
        consumer: a new kind must not reach a caller as an unhandled 500."""
        from routers.cowork_agent.bff.visualizer import _GITHUB_FAILURES

        self.assertEqual(set(_GITHUB_FAILURES), set(ERROR_KINDS))
        for kind, (status, code) in _GITHUB_FAILURES.items():
            with self.subTest(kind):
                self.assertIn(status, (400, 403, 404, 502, 503))
                self.assertTrue(code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
