"""W10 — workitem timeline events, and the ``append_jsonl`` contract (O-D).

Three things are held here, and the first is the one that has already
gone wrong once in this codebase.

**1. The emitter may not outrun the schema.** ``timeline.schema.json`` is
a ``oneOf`` over declared branches: a line whose ``type`` matches no
branch is *invalid*, not merely undescribed. Defect R3a was exactly
that — ``sinks/timeline.py`` started emitting ``todo.status_changed``
and the ``oneOf`` rejected every one of those lines, silently, because
nothing crossed the sink/schema boundary in a test. So
:class:`SchemaVocabularyTests` pins the two sides against each other
structurally, and :class:`EmitterOutputTests` validates **real emitter
output** — every line produced by driving the actual stores, never a
hand-written fixture, because a fixture only ever proves what its author
believed the emitter writes.

**2. The store is the event source, not the routes.** Five entry points
reach a workitem record, and an emit bolted onto each is an emit one of
them will eventually be added without. :class:`StoreEmissionTests` drives
the stores directly and :class:`RouteEmissionTests` drives HTTP, and both
see the same lines. The single exception is assigning an *adopted* item,
which writes to GitHub and deliberately writes nothing locally (§5.3) —
there is no store write to hang the event on, so the route emits it, and
:meth:`RouteEmissionTests.test_assigning_on_github_is_still_recorded`
is what keeps that from being forgotten.

**3. A failure to log may not fail the write.** The timeline is derived,
append-only history; ``workitems.json`` is authored state in the synced
tier. :class:`EmissionNeverFailsTheWriteTests` breaks the append in every
way it can be broken and asserts the write still stands.

:class:`AppendJsonlContractTests` covers the O-D half: the docstring on
``atomic_write.append_jsonl`` claimed *"exactly one writer — the
watcher"* long after there were two, and W10 adds a fourth. The tests
there assert the *behaviour* the corrected contract promises, so a future
edit that narrows the claim again has something to contradict it.

Never touches the real ``~/xo-projects`` or ``~/.quirq``: both roots are
redirected into a temp dir.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

try:  # pragma: no cover - exercised by its absence, not by a branch
    from jsonschema import Draft7Validator
except ImportError:  # pragma: no cover
    Draft7Validator = None  # type: ignore[assignment]

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.cowork_agent import github_poller, project_layout
from services.cowork_agent.connectors import github_issue_actions
from services.cowork_agent.visualizer import (
    atomic_write,
    workitem_claims,
    workitems_store,
)
from services.cowork_agent.visualizer.ingest.events import (
    WORKITEM_ACTIONS,
    WorkitemEvent,
)
from services.cowork_agent.visualizer.sinks import timeline


_SKIP_REASON = (
    "jsonschema is not installed — "
    "uv pip install --python venv/bin/python -r requirements-dev.txt"
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    ROOT / "services" / "cowork_agent" / "visualizer" / "schema"
    / "timeline.schema.json"
)

REPO = "cjpais/Handy"
NODE_ID = "I_kwDOJ0000M6ABCDEF"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _github_ref(number: int = 42, node_id: str = NODE_ID) -> dict:
    return {
        "repo": REPO,
        "number": number,
        "node_id": node_id,
        "url": f"https://github.com/{REPO}/issues/{number}",
    }


def _declared_types(schema: dict) -> list[str]:
    """Every ``type`` const the schema's ``oneOf`` actually reaches.

    Walked from ``oneOf`` rather than read off ``definitions``, because
    the two are not the same set: a definition nothing references is a
    branch that does not exist as far as validation is concerned, and
    that is precisely how a "declared" event type stays rejected.
    """
    out: list[str] = []
    for branch in schema.get("oneOf", []):
        ref = branch.get("$ref", "")
        name = ref.rsplit("/", 1)[-1]
        definition = schema.get("definitions", {}).get(name)
        if not isinstance(definition, dict):
            continue
        const = definition.get("properties", {}).get("type", {}).get("const")
        if isinstance(const, str):
            out.append(const)
    return out


class _TimelineCase(unittest.TestCase):
    """A project on redirected roots, with both stores addressable."""

    PROJECT = "demo"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.xo = tmp / "xo-projects" / self.PROJECT / ".xo"
        self.xo.mkdir(parents=True)
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(tmp / "xo-projects"),
            "QUIRQ_STATE_ROOT": str(tmp / "quirq"),
        })
        env.start()
        self.addCleanup(env.stop)

    # ── the two documents, and the derived view they feed ────────────

    @property
    def path(self) -> Path:
        return self.xo / "workitems.json"

    def runtime_dir(self) -> Path:
        """Resolved through the chokepoint, so the assertions land on
        wherever the code genuinely writes rather than on a second
        hand-built copy of the layout."""
        found = project_layout.runtime_dir_for_project(self.PROJECT)
        assert found is not None, "demo project has no runtime home"
        return found

    @property
    def claims_path(self) -> Path:
        return workitem_claims.claims_path_for(self.runtime_dir())

    def lines(self) -> list[dict]:
        path = self.runtime_dir() / "timeline.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(raw)
            for raw in path.read_text(encoding="utf-8").splitlines()
            if raw.strip()
        ]

    def types(self) -> list[str]:
        return [line["type"] for line in self.lines()]

    def workitem_lines(self) -> list[dict]:
        return [ln for ln in self.lines() if ln["type"].startswith("workitem.")]

    # ── helpers ──────────────────────────────────────────────────────

    def create(self, **kwargs) -> dict:
        payload = {"runtime": "claude_code", "title": "a workitem"}
        payload.update(kwargs)
        return workitems_store.create_workitem(self.path, **payload)

    def assertLinesValid(self, label: str = "emitter output") -> list[dict]:
        """Validate every line on disk against ``timeline.schema.json``.

        Real emitter output: whatever the stores actually wrote in this
        test, parsed back off the file, not a fixture retyped by hand.
        """
        if Draft7Validator is None:  # pragma: no cover - skip guard
            self.skipTest(_SKIP_REASON)
        validator = Draft7Validator(_schema())
        lines = self.lines()
        for index, line in enumerate(lines):
            errors = sorted(
                validator.iter_errors(line), key=lambda e: list(e.path)
            )
            if errors:
                detail = "\n".join(f"  {e.message}" for e in errors)
                self.fail(
                    f"{label}: line {index} ({line.get('type')!r}) fails "
                    f"timeline.schema.json:\n{json.dumps(line)}\n{detail}"
                )
        return lines


# ── 1. emitter and schema, pinned to each other ──────────────────────────────


@unittest.skipIf(Draft7Validator is None, _SKIP_REASON)
class SchemaVocabularyTests(unittest.TestCase):
    """R3a, structurally: the two sides cannot drift apart silently.

    The emitter renders ``workitem.<action>`` for exactly
    ``WORKITEM_ACTIONS`` and drops everything else, and the schema
    declares exactly one reachable branch per action. Asserting *both*
    directions is the point — a missing branch is R3a, and an extra
    branch is a type nothing writes, which is how a schema starts
    describing a system that does not exist.
    """

    def test_the_schema_is_a_valid_draft_07_document(self) -> None:
        """The second trap from the R3a session: a bare string dropped
        into ``definitions`` is not a schema, and every validation after
        it is meaningless."""
        Draft7Validator.check_schema(_schema())

    def test_every_emitted_action_has_a_reachable_branch(self) -> None:
        declared = set(_declared_types(_schema()))
        for action in sorted(WORKITEM_ACTIONS):
            with self.subTest(action=action):
                self.assertIn(
                    f"workitem.{action}", declared,
                    "the emitter can render this type and the schema's "
                    "oneOf would reject it — defect R3a exactly",
                )

    def test_the_schema_declares_no_workitem_type_nothing_emits(self) -> None:
        declared = {
            t for t in _declared_types(_schema()) if t.startswith("workitem.")
        }
        self.assertEqual(
            declared, {f"workitem.{a}" for a in WORKITEM_ACTIONS},
            "the schema and ingest.events.WORKITEM_ACTIONS are the two "
            "halves of one vocabulary; neither may grow alone",
        )

    def test_the_branches_are_declared_exactly_once_each(self) -> None:
        declared = _declared_types(_schema())
        duplicates = sorted({t for t in declared if declared.count(t) > 1})
        self.assertEqual(
            duplicates, [],
            "oneOf means exactly one branch may match; a duplicated const "
            "makes every line of that type invalid",
        )

    def test_an_undeclared_workitem_type_is_still_rejected(self) -> None:
        """The vocabulary is closed. A schema that accepts anything is
        worse than one that is stale, because it reads as a guarantee."""
        validator = Draft7Validator(_schema())
        self.assertFalse(validator.is_valid(
            {"ts": "2026-09-08T12:00:00Z", "type": "workitem.invented",
             "workitem_id": "w1"}
        ))
        self.assertFalse(validator.is_valid(
            {"ts": "2026-09-08T12:00:00Z", "type": "workitem.created"}
        ), "workitem_id is required on every branch")
        self.assertFalse(validator.is_valid(
            {"ts": "2026-09-08T12:00:00Z", "type": "workitem.claimed",
             "workitem_id": "w1"}
        ), "a claim with no session claims nothing")
        self.assertFalse(validator.is_valid(
            {"ts": "2026-09-08T12:00:00Z", "type": "workitem.assigned",
             "workitem_id": "w1"}
        ), "assignee is required and nullable — absent is not 'nobody'")


class EmitterDropsWhatItCannotDeclareTests(unittest.TestCase):
    """The renderer's own guard, at the unit level."""

    def test_an_action_outside_the_vocabulary_renders_nothing(self) -> None:
        rendered = timeline._emit_event(
            WorkitemEvent(ts="t", action="promoted", workitem_id="w1")
        )
        self.assertIsNone(
            rendered,
            "rendering it would put a line in the log that the schema's "
            "oneOf rejects — the R3a failure, reached from the other side",
        )

    def test_an_event_with_no_workitem_id_renders_nothing(self) -> None:
        self.assertIsNone(timeline._emit_event(
            WorkitemEvent(ts="t", action="created", workitem_id="")
        ))

    def test_unknown_session_and_runtime_are_omitted_not_placeheld(self) -> None:
        rendered = timeline._emit_event(
            WorkitemEvent(ts="t", action="deleted", workitem_id="w1")
        )
        self.assertNotIn("session_id", rendered)
        self.assertNotIn("runtime", rendered)


# ── 2. real emitter output, every type ───────────────────────────────────────


@unittest.skipIf(Draft7Validator is None, _SKIP_REASON)
class EmitterOutputTests(_TimelineCase):
    """Drive the real stores until all eight types exist, then validate.

    Not fixtures. The lines are read back off ``timeline.jsonl`` after
    the stores wrote them, which is the only version of this test that
    can catch the emitter and the schema disagreeing.
    """

    def produce_every_type(self) -> None:
        local = self.create(title="local one")
        workitems_store.update_workitem(
            self.path, local["id"], status="closed", state_reason="completed",
        )
        workitems_store.update_workitem(self.path, local["id"], status="open")
        workitems_store.update_workitem(self.path, local["id"], assignee="ada")
        workitem_claims.claim_workitem(
            self.claims_path, local["id"], session_id="sess-a", runtime="codex",
        )
        workitem_claims.release_workitem(self.claims_path, local["id"])
        workitems_store.delete_workitem(
            self.path, local["id"], deleted_by="claude_code",
        )
        # Adoption, which is where ``created`` + ``adopted`` come from.
        workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=_github_ref(),
            title="Rate-limit the poller", labels=["infra"],
        )

    def test_every_declared_type_is_actually_produced(self) -> None:
        self.produce_every_type()
        produced = {ln["type"] for ln in self.workitem_lines()}
        self.assertEqual(
            produced, {f"workitem.{a}" for a in WORKITEM_ACTIONS},
            "a type nothing produces is a branch nothing validates; this "
            "test is what makes the validation below load-bearing",
        )

    def test_real_emitter_output_validates_line_by_line(self) -> None:
        self.produce_every_type()
        lines = self.assertLinesValid("the stores' own output")
        self.assertEqual(len(lines), 9, [ln["type"] for ln in lines])

    def test_the_payloads_carry_what_a_reader_needs(self) -> None:
        self.produce_every_type()
        by_type = {ln["type"]: ln for ln in self.workitem_lines()}

        created = by_type["workitem.created"]
        self.assertEqual(created["kind"], "github", "the last one wins here")
        self.assertEqual(created["runtime"], "claude_code")

        adopted = by_type["workitem.adopted"]
        self.assertEqual(adopted["issue"], {"repo": REPO, "number": 42})

        self.assertEqual(by_type["workitem.closed"]["state_reason"], "completed")
        self.assertEqual(by_type["workitem.assigned"]["assignee"], "ada")
        self.assertEqual(by_type["workitem.claimed"]["session_id"], "sess-a")
        self.assertEqual(by_type["workitem.claimed"]["runtime"], "codex")
        self.assertEqual(by_type["workitem.deleted"]["runtime"], "claude_code")

    def test_todo_lines_and_workitem_lines_share_one_file(self) -> None:
        """The two stores append to the same log, and both stay valid —
        which is the whole reason O-D had to be fixed first."""
        from services.cowork_agent.visualizer import todos_store

        todos_store.create_todo(
            self.xo / "todos.json", runtime="codex", content="a step",
        )
        self.create(title="the work")
        self.assertEqual(
            self.types(), ["todo.added", "workitem.created"],
            "one file, two appenders, in call order",
        )
        self.assertLinesValid("two stores appending to one timeline")


# ── 3. where the events come from ────────────────────────────────────────────


class StoreEmissionTests(_TimelineCase):
    """Emission belongs to the write, so no caller path can miss it."""

    def test_a_local_create_says_local(self) -> None:
        item = self.create()
        line, = self.workitem_lines()
        self.assertEqual(line["type"], "workitem.created")
        self.assertEqual(line["workitem_id"], item["id"])
        self.assertEqual(line["kind"], "local")
        self.assertEqual(line["title"], "a workitem")

    def test_creating_an_already_adopted_item_records_both(self) -> None:
        """One call, two things that happened: a record exists and it
        stands for an issue."""
        self.create(
            title="Rate-limit the poller",
            source={"kind": "github", "github": _github_ref()},
        )
        self.assertEqual(
            self.types(), ["workitem.created", "workitem.adopted"],
        )

    def test_adopting_into_a_new_record_records_both(self) -> None:
        workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=_github_ref(),
            title="Rate-limit the poller",
        )
        self.assertEqual(
            self.types(), ["workitem.created", "workitem.adopted"],
        )

    def test_adopting_an_existing_local_record_records_only_adopted(self) -> None:
        local = self.create(title="my note")
        before = len(self.lines())
        workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=_github_ref(),
            workitem_id=local["id"], title="Rate-limit the poller",
        )
        new = self.lines()[before:]
        self.assertEqual([ln["type"] for ln in new], ["workitem.adopted"])
        self.assertEqual(new[0]["workitem_id"], local["id"],
                         "the record kept its id, and so does its history")

    def test_a_re_fired_adopt_mints_no_second_history(self) -> None:
        workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=_github_ref(),
            title="Rate-limit the poller",
        )
        before = self.types()
        workitems_store.adopt_workitem(
            self.path, runtime="claude_code", github=_github_ref(),
            title="Rate-limit the poller",
        )
        self.assertEqual(self.types(), before,
                         "the idempotent return wrote nothing, so it logs "
                         "nothing")

    def test_closing_and_reopening_are_two_events(self) -> None:
        item = self.create()
        workitems_store.update_workitem(
            self.path, item["id"], status="closed", state_reason="not_planned",
        )
        workitems_store.update_workitem(self.path, item["id"], status="open")
        lines = self.workitem_lines()[1:]
        self.assertEqual(
            [ln["type"] for ln in lines],
            ["workitem.closed", "workitem.reopened"],
        )
        self.assertEqual(lines[0]["state_reason"], "not_planned",
                         "cancelled is GitHub's state_reason, not a status")

    def test_re_closing_a_closed_item_emits_nothing(self) -> None:
        item = self.create(status="closed")
        before = self.types()
        workitems_store.update_workitem(self.path, item["id"], status="closed")
        self.assertEqual(self.types(), before)

    def test_clearing_an_assignee_is_an_assignment_to_nobody(self) -> None:
        item = self.create(assignee="ada")
        workitems_store.update_workitem(self.path, item["id"], assignee=None)
        line = self.workitem_lines()[-1]
        self.assertEqual(line["type"], "workitem.assigned")
        self.assertIsNone(
            line["assignee"],
            "null is the un-assignment; an absent key could not say it",
        )

    def test_an_edit_with_no_declared_type_emits_nothing(self) -> None:
        item = self.create()
        before = self.types()
        workitems_store.update_workitem(
            self.path, item["id"], title="renamed", labels=["infra"],
        )
        self.assertEqual(
            self.types(), before,
            "§8 declares no workitem.renamed; inventing one would put a "
            "type in the log that the schema rejects",
        )

    def test_a_no_op_update_emits_nothing(self) -> None:
        item = self.create()
        before = self.types()
        workitems_store.update_workitem(self.path, item["id"], title="a workitem")
        self.assertEqual(self.types(), before)

    def test_a_tombstone_is_an_event_and_a_second_delete_is_not(self) -> None:
        item = self.create()
        workitems_store.delete_workitem(
            self.path, item["id"], deleted_by="claude_code",
        )
        self.assertEqual(self.types()[-1], "workitem.deleted")
        before = self.types()
        workitems_store.delete_workitem(self.path, item["id"])
        self.assertEqual(self.types(), before, "nothing was tombstoned twice")

    def test_a_claim_and_its_release_bracket_the_work(self) -> None:
        item = self.create()
        workitem_claims.claim_workitem(
            self.claims_path, item["id"], session_id="sess-a", runtime="codex",
        )
        workitem_claims.release_workitem(self.claims_path, item["id"])
        claimed, released = self.workitem_lines()[1:]
        self.assertEqual(claimed["type"], "workitem.claimed")
        self.assertEqual(released["type"], "workitem.released")
        self.assertEqual(
            released["session_id"], "sess-a",
            "who stopped is read off the claim, because the implicit "
            "releases do not know the session",
        )
        self.assertEqual(released["runtime"], "codex")

    def test_re_claiming_is_a_second_event(self) -> None:
        """``in_progress`` is derived and never stored, so this log is the
        only place a hand-over from one session to another exists."""
        item = self.create()
        workitem_claims.claim_workitem(
            self.claims_path, item["id"], session_id="sess-a", runtime="codex",
        )
        workitem_claims.claim_workitem(
            self.claims_path, item["id"], session_id="sess-b", runtime="codex",
        )
        claims = [ln for ln in self.lines() if ln["type"] == "workitem.claimed"]
        self.assertEqual([ln["session_id"] for ln in claims], ["sess-a", "sess-b"])

    def test_releasing_an_unclaimed_workitem_emits_nothing(self) -> None:
        item = self.create()
        before = self.types()
        self.assertFalse(
            workitem_claims.release_workitem(self.claims_path, item["id"])
        )
        self.assertEqual(self.types(), before)

    def test_a_lapsed_claim_emits_nothing_by_design(self) -> None:
        """§5.4: a session dying is an absence of observation, not an
        event. Nothing runs at that moment, and manufacturing a line
        would need the cleanup path the derivation exists to avoid."""
        item = self.create()
        workitem_claims.claim_workitem(
            self.claims_path, item["id"], session_id="sess-a", runtime="codex",
        )
        before = self.types()
        derived = workitem_claims.in_progress_ids(
            workitem_claims.read_claims(self.claims_path),
            live_sessions=(), grace=0.0,
        )
        self.assertEqual(derived, frozenset(), "the claim has lapsed")
        self.assertEqual(self.types(), before, "and nothing was written")


# ── 4. a log may never fail the write it describes ───────────────────────────


class EmissionNeverFailsTheWriteTests(_TimelineCase):
    """The record is on disk before the append is attempted.

    Raising here would turn a workitem that exists into a ``500``, and
    the caller would then retry a create that already succeeded. Every
    failure mode below is therefore swallowed and logged.
    """

    def test_a_failing_append_does_not_fail_a_create(self) -> None:
        # Patched on the sink, which imported the symbol by value — the
        # only binding the store's call actually goes through.
        with patch.object(
            timeline, "append_jsonl", side_effect=OSError("disk full")
        ):
            item = self.create()
        self.assertIn(item["id"], json.loads(self.path.read_text("utf-8"))["items"])
        self.assertEqual(self.lines(), [], "the line is the only casualty")

    def test_a_failing_append_does_not_fail_a_claim(self) -> None:
        item = self.create()
        with patch.object(
            timeline, "apply", side_effect=RuntimeError("sink exploded")
        ):
            claim = workitem_claims.claim_workitem(
                self.claims_path, item["id"], session_id="s", runtime="r",
            )
        self.assertEqual(claim["session_id"], "s")
        self.assertIn(item["id"], workitem_claims.read_claims(self.claims_path))

    def test_a_failing_append_does_not_fail_a_delete(self) -> None:
        item = self.create()
        with patch.object(
            timeline, "apply", side_effect=RuntimeError("sink exploded")
        ):
            self.assertTrue(
                workitems_store.delete_workitem(self.path, item["id"])
            )
        stored = json.loads(self.path.read_text("utf-8"))["items"][item["id"]]
        self.assertIsNotNone(stored["deleted_at"])

    def test_a_project_with_no_runtime_home_is_a_skipped_write(self) -> None:
        """The same answer the watcher gives: nowhere to put a derived
        view is a skip, not an error."""
        elsewhere = Path(self._tmp.name) / "not-a-project" / ".xo"
        elsewhere.mkdir(parents=True)
        item = workitems_store.create_workitem(
            elsewhere / "workitems.json", runtime="codex", title="orphan",
        )
        self.assertTrue(item["id"])

    def test_apply_quiet_tolerates_a_missing_root(self) -> None:
        self.assertEqual(
            timeline.apply_quiet(
                None, [WorkitemEvent(ts="t", action="created", workitem_id="w")]
            ),
            [],
        )


# ── 5. over HTTP — the same lines, whichever route got there ─────────────────


class RouteEmissionTests(unittest.TestCase):
    """Every workitem route lands the same events the stores emit."""

    PROJECT = "demo"
    PID = "00000001-0000-4000-8000-000000000001"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.quirq = tmp / "quirq"
        self.xo = tmp / "xo-projects" / self.PROJECT / ".xo"
        self.xo.mkdir(parents=True)
        (self.xo / "project.json").write_text(json.dumps({
            "schema": 2,
            "pid": self.PID,
            "name": self.PROJECT,
            "owner_user_id": "local",
            "created_at": "2026-01-01T00:00:00Z",
            "git": {"remote_url": f"https://github.com/{REPO}.git"},
        }), encoding="utf-8")
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(tmp / "xo-projects"),
            "QUIRQ_STATE_ROOT": str(self.quirq),
        })
        env.start()
        self.addCleanup(env.stop)

        github_poller.reset_state()
        self.addCleanup(github_poller.reset_state)
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

    def lines(self) -> list[dict]:
        path = self.quirq / "projects" / self.PID / "timeline.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(raw)
            for raw in path.read_text(encoding="utf-8").splitlines()
            if raw.strip()
        ]

    def types(self) -> list[str]:
        return [ln["type"] for ln in self.lines()]

    def create(self, **body) -> dict:
        payload = {"runtime": "claude_code", "title": "a workitem"}
        payload.update(body)
        res = self.client.post(f"{self.base}/workitems", json=payload)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    def test_the_crud_routes_reach_the_same_emitter(self) -> None:
        item = self.create()
        self.assertEqual(self.types(), ["workitem.created"])
        self.assertEqual(self.lines()[0]["workitem_id"], item["id"])

    def test_closing_over_patch_also_releases_the_claim(self) -> None:
        """§5.4's implicit release, seen from the log: one request, two
        events, and neither is emitted by the route."""
        item = self.create()
        self.client.post(
            f"{self.base}/workitems/{item['id']}/claim",
            json={"session_id": "sess-a", "runtime": "claude_code"},
        )
        res = self.client.patch(
            f"{self.base}/workitems/{item['id']}", json={"status": "closed"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(
            self.types(),
            ["workitem.created", "workitem.claimed", "workitem.closed",
             "workitem.released"],
        )

    def test_deleting_releases_the_claim_too(self) -> None:
        item = self.create()
        self.client.post(
            f"{self.base}/workitems/{item['id']}/claim",
            json={"session_id": "sess-a", "runtime": "claude_code"},
        )
        res = self.client.delete(
            f"{self.base}/workitems/{item['id']}?runtime=claude_code"
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(
            self.types()[-2:], ["workitem.deleted", "workitem.released"],
        )

    def test_the_explicit_release_route_records_it(self) -> None:
        item = self.create()
        self.client.post(
            f"{self.base}/workitems/{item['id']}/claim",
            json={"session_id": "sess-a", "runtime": "claude_code"},
        )
        res = self.client.delete(f"{self.base}/workitems/{item['id']}/claim")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self.types()[-1], "workitem.released")

    def test_assigning_a_local_item_comes_from_the_store(self) -> None:
        item = self.create()
        with patch.dict(os.environ, {"CODER_WORKSPACE_OWNER_NAME": "ada"}):
            res = self.client.put(
                f"{self.base}/workitems/{item['id']}/assignee",
                json={"assignee": "me"},
            )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self.types()[-1], "workitem.assigned")
        self.assertEqual(self.lines()[-1]["assignee"], "ada")

    def test_assigning_on_github_is_still_recorded(self) -> None:
        """The one event a route emits, and why: the write went to
        GitHub and nothing was written to ``.xo/`` (§5.3), so there is no
        store call to hang it on — yet D1 makes *this* the assignment
        that actually routes work."""
        async def _fetch(repo, number, **kwargs):
            return github_issue_actions.IssueResult(
                ok=True, repo=str(repo), number=number, issue={
                    "node_id": NODE_ID, "number": 42, "repo": REPO,
                    "title": "Rate-limit the poller", "state": "open",
                    "state_reason": None, "assignees": [], "labels": ["infra"],
                    "url": f"https://github.com/{REPO}/issues/42",
                    "updated_at": "2026-09-08T12:00:00Z",
                },
            )

        async def _assign(repo, number, wanted, **kwargs):
            return github_issue_actions.AssignResult(
                ok=True, repo=str(repo), number=number, assignees=list(wanted),
            )

        with patch.object(github_issue_actions, "fetch_issue", _fetch):
            adopted = self.client.post(
                f"{self.base}/github/issues/42/adopt",
                json={"runtime": "claude_code"},
            )
        self.assertEqual(adopted.status_code, 201, adopted.text)
        workitem_id = adopted.json()["id"]

        with patch.object(github_issue_actions, "set_assignees", _assign):
            res = self.client.put(
                f"{self.base}/workitems/{workitem_id}/assignee",
                json={"assignee": "ada"},
            )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(
            self.types(),
            ["workitem.created", "workitem.adopted", "workitem.assigned"],
        )
        self.assertEqual(self.lines()[-1]["assignee"], "ada")
        self.assertNotIn(
            "assignee",
            json.loads((self.xo / "workitems.json").read_text("utf-8"))
            ["items"][workitem_id],
            "the timeline records it; the synced tier still must not",
        )

    @unittest.skipIf(Draft7Validator is None, _SKIP_REASON)
    def test_everything_the_routes_wrote_validates(self) -> None:
        item = self.create()
        self.client.post(
            f"{self.base}/workitems/{item['id']}/claim",
            json={"session_id": "sess-a", "runtime": "claude_code"},
        )
        self.client.patch(
            f"{self.base}/workitems/{item['id']}", json={"status": "closed"},
        )
        self.client.delete(f"{self.base}/workitems/{item['id']}?runtime=codex")
        validator = Draft7Validator(_schema())
        for line in self.lines():
            with self.subTest(type=line["type"]):
                self.assertTrue(
                    validator.is_valid(line), list(validator.iter_errors(line))
                )

    def test_the_new_types_survive_the_read_back(self) -> None:
        """``GET /timeline`` must serve a workitem line, not skip it —
        the response model allows the per-type extras the ``oneOf`` does."""
        item = self.create()
        res = self.client.get(f"{self.base}/timeline")
        self.assertEqual(res.status_code, 200, res.text)
        events = res.json()["events"]
        self.assertEqual([e["type"] for e in events], ["workitem.created"])
        self.assertEqual(events[0]["workitem_id"], item["id"])


# ── 6. O-D — what append_jsonl actually guarantees ───────────────────────────


class AppendJsonlContractTests(unittest.TestCase):
    """The corrected contract, asserted as behaviour rather than prose.

    ``append_jsonl`` claimed *"exactly one writer — the watcher"* from
    the day the todos API became the todo event source, and W10 adds a
    fourth appender. The docstring is the deliverable; these are what
    stop it being narrowed back.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "timeline.jsonl"

    def read(self) -> list[str]:
        return [
            raw for raw in self.path.read_text("utf-8").splitlines() if raw.strip()
        ]

    def test_concurrent_appenders_lose_nothing_and_tear_nothing(self) -> None:
        """The guarantee a new writer may rely on: ``O_APPEND`` means
        every write lands at end-of-file, so batches interleave in some
        order but no line is lost or half-written."""
        writers = 6
        per_writer = 40
        barrier = threading.Barrier(writers)

        def append(tag: int) -> None:
            barrier.wait()
            for n in range(per_writer):
                atomic_write.append_jsonl(
                    self.path, [{"writer": tag, "n": n, "pad": "x" * 200}]
                )

        threads = [threading.Thread(target=append, args=(i,)) for i in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        lines = self.read()
        self.assertEqual(len(lines), writers * per_writer)
        parsed = [json.loads(raw) for raw in lines]  # no torn line survives this
        self.assertEqual(
            sorted((row["writer"], row["n"]) for row in parsed),
            sorted((w, n) for w in range(writers) for n in range(per_writer)),
            "every writer's every line is present exactly once",
        )

    def test_each_writers_own_lines_keep_their_order(self) -> None:
        """Ordering is guaranteed *within* a writer and nowhere else —
        which is why a reader that needs chronology sorts by ``ts``."""
        for n in range(5):
            atomic_write.append_jsonl(self.path, [{"n": n}])
        self.assertEqual(
            [json.loads(raw)["n"] for raw in self.read()], [0, 1, 2, 3, 4],
        )

    def test_a_batch_is_written_whole(self) -> None:
        atomic_write.append_jsonl(
            self.path, [{"n": 1}, {"n": 2}, {"n": 3}],
        )
        self.assertEqual([json.loads(raw)["n"] for raw in self.read()], [1, 2, 3])

    def test_the_file_always_ends_in_a_newline(self) -> None:
        """Otherwise the next appender extends the last line instead of
        starting one — the tearing this contract says cannot happen."""
        atomic_write.append_jsonl(self.path, [{"n": 1}])
        self.assertTrue(self.path.read_bytes().endswith(b"\n"))
        atomic_write.append_jsonl(self.path, [{"n": 2}])
        self.assertEqual(len(self.read()), 2)

    def test_an_empty_batch_does_not_create_the_file(self) -> None:
        atomic_write.append_jsonl(self.path, [])
        self.assertFalse(self.path.exists())

    def test_the_docstring_no_longer_claims_a_single_writer(self) -> None:
        """O-D itself. The defect was never the behaviour — it was a
        contract that told the next author interleaving could not happen,
        while three other writers were already appending."""
        doc = atomic_write.append_jsonl.__doc__ or ""
        self.assertIn(
            "many writers", doc,
            "the old claim was that there was one; it must now say there "
            "are several, because there are",
        )
        self.assertIn("O_APPEND", doc, "and say what that does guarantee")
        self.assertIn(
            "Not guaranteed", doc,
            "the half a new writer most needs: ordering between writers, "
            "batch contiguity, and the buffer-sized limit on line integrity",
        )
        for owner in ("todos_store", "workitems_store", "workitem_claims"):
            self.assertIn(owner, doc, "and name the writers it knows about")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
