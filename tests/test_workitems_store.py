"""Workitems: the record, the store, and the O-E failure it must not repeat.

Three groups, in ascending order of how much they would hurt if they
broke (docs/workitems-plan.md §10, rows W1 and W2):

* **the schema rejects** — a schema that accepts everything is worse
  than one that is stale, so every case below that says "must not
  validate" is doing the real work. The positive cases are fed from the
  store's own output, so the record and its validator cannot drift.
* **the store's CRUD** — tombstones instead of deletes, ``open``/
  ``closed`` and nothing else, and the §5.3 asymmetry: an adopted item
  does not carry the four fields GitHub owns, and trying to write one is
  refused rather than accepted and left to go stale.
* **a corrupt document raises** — the O-E defect
  (``docs/OUTSTANDING.md``), where ``todos_store`` maps an unparseable
  file to "no sessions" and the next create silently discards what the
  file held. Every entry point is checked, and the bytes on disk are
  asserted unchanged afterwards: refusing to write is only half of it,
  the other half is not damaging the evidence.

Never touches the real ``~/xo-projects`` or ``~/.quirq``: both roots are
redirected into a temp dir and the workitems file is addressed by path.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

try:  # pragma: no cover - exercised by its absence, not by a branch
    from jsonschema import Draft7Validator
except ImportError:  # pragma: no cover
    Draft7Validator = None  # type: ignore[assignment]

from services.cowork_agent.visualizer import workitems_store
from services.cowork_agent.visualizer.workitems_store import (
    UNSET,
    WorkitemsStoreError,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    ROOT / "services" / "cowork_agent" / "visualizer" / "schema" / "workitems.schema.json"
)

_SKIP_REASON = (
    "jsonschema is not installed — "
    "uv pip install --python venv/bin/python -r requirements-dev.txt"
)

GITHUB_REF = {
    "repo": "dwivedi-ai/xo-cowork-api",
    "number": 42,
    "node_id": "I_kwDOABCD1234",
    "url": "https://github.com/dwivedi-ai/xo-cowork-api/issues/42",
}


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class _WorkitemsCase(unittest.TestCase):
    """Temp project + redirected roots, shared by every case below."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.xo_dir = tmp / "xo-projects" / "demo" / ".xo"
        self.xo_dir.mkdir(parents=True)
        self.path = self.xo_dir / "workitems.json"
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(tmp / "xo-projects"),
            "QUIRQ_STATE_ROOT": str(tmp / "quirq"),
        })
        env.start()
        self.addCleanup(env.stop)

    # ── helpers ──────────────────────────────────────────────────────
    def document(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def create(self, **kwargs) -> dict:
        kwargs.setdefault("runtime", "codex")
        kwargs.setdefault("title", "Rate-limit the GitHub poller")
        return workitems_store.create_workitem(self.path, **kwargs)

    def adopt(self, **kwargs) -> dict:
        kwargs.setdefault("source", {"kind": "github", "github": dict(GITHUB_REF)})
        return self.create(**kwargs)


# ── 1. the schema ─────────────────────────────────────────────────────────────


@unittest.skipIf(Draft7Validator is None, _SKIP_REASON)
class SchemaFileTests(unittest.TestCase):
    """The file itself, and the conventions the other twelve follow."""

    def test_it_is_a_valid_draft7_schema(self) -> None:
        Draft7Validator.check_schema(_schema())

    def test_it_declares_draft7_and_the_id_convention(self) -> None:
        schema = _schema()
        self.assertEqual(
            schema["$schema"], "http://json-schema.org/draft-07/schema#"
        )
        self.assertEqual(schema["$id"], "xo/workitems.schema.json")

    def test_the_store_stamps_the_schemas_own_id(self) -> None:
        """The ``$schema`` a document carries is the schema's ``$id`` — a
        logical id, never a path into a ``.xo/schema/`` directory that has
        never existed (syncplan T16)."""
        self.assertEqual(workitems_store._SCHEMA_REF, _schema()["$id"])

    def test_the_declared_version_matches_the_store(self) -> None:
        self.assertEqual(
            _schema()["properties"]["schema"]["const"],
            workitems_store.WORKITEMS_SCHEMA,
        )

    def test_the_status_enum_is_githubs_two_values_and_no_more(self) -> None:
        """D7: no ``in_progress``, no ``blocked``. In progress is derived
        from a live claim; a stored flag lies when the process dies."""
        enum = _schema()["definitions"]["workitem"]["properties"]["status"]["enum"]
        self.assertEqual(sorted(enum), ["closed", "open"])
        self.assertEqual(set(enum), set(workitems_store.VALID_STATUSES))

    def test_the_state_reason_enum_mirrors_github(self) -> None:
        enum = _schema()["definitions"]["workitem"]["properties"]["state_reason"]["enum"]
        self.assertEqual(
            sorted(v for v in enum if v is not None),
            ["completed", "not_planned", "reopened"],
        )
        self.assertIn(None, enum)


@unittest.skipIf(Draft7Validator is None, _SKIP_REASON)
class _ValidatingCase(_WorkitemsCase):
    """Adds ``assertValid`` / ``assertInvalid`` against the schema."""

    def assertValid(self, document: object, label: str) -> None:
        validator = Draft7Validator(_schema())
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
        if errors:
            detail = "\n".join(
                f"  at /{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                for e in errors
            )
            self.fail(f"{label} fails workitems.schema.json:\n{detail}")

    def assertInvalid(self, document: object, label: str) -> None:
        if Draft7Validator(_schema()).is_valid(document):
            self.fail(f"{label} was accepted by workitems.schema.json but must not be")

    def wrap(self, item: dict) -> dict:
        return {
            "$schema": "xo/workitems.schema.json",
            "schema": 1,
            "updated_at": "2026-09-08T12:00:00Z",
            "items": {item["id"]: item},
        }

    def local_item(self) -> dict:
        return copy.deepcopy(self.create())

    def github_item(self) -> dict:
        return copy.deepcopy(self.adopt())


class SchemaAcceptsRealOutputTests(_ValidatingCase):
    """Positive cases, all fed from the store rather than hand-written —
    a schema validated only against a fixture drifts from its writer."""

    def test_a_freshly_created_local_document_validates(self) -> None:
        self.create()
        self.assertValid(self.document(), "a local workitem")

    def test_an_adopted_document_validates(self) -> None:
        self.adopt()
        self.assertValid(self.document(), "an adopted workitem")

    def test_a_fully_populated_local_document_validates(self) -> None:
        item = self.create(
            body="why",
            labels=["infra", "needs design"],
            status="closed",
            state_reason="not_planned",
            assignee="dwivedi-ai",
            todo_ids=["abc12345"],
            session_ids=["hermes:a:web:aaaaaaa1"],
        )
        workitems_store.delete_workitem(self.path, item["id"], deleted_by="codex")
        self.assertValid(self.document(), "a closed, tombstoned workitem")

    def test_an_adopted_item_may_carry_lazily_fetched_labels(self) -> None:
        """§6.2: labels are kept out of the poll query because a nested
        connection was measured to halve the project ceiling, so they are
        fetched lazily at adoption and land in this file."""
        self.adopt(labels=["bug"])
        self.assertValid(self.document(), "an adopted workitem with labels")

    def test_a_document_with_no_items_yet_validates(self) -> None:
        item = self.create()
        workitems_store.delete_workitem(self.path, item["id"])
        doc = self.document()
        doc["items"] = {}
        self.assertValid(doc, "an empty items map")


class SchemaRejectsTests(_ValidatingCase):
    """The half that earns its keep. Each case is a document that must
    NOT validate — a schema that accepts everything says nothing."""

    def test_it_rejects_an_undeclared_top_level_key(self) -> None:
        self.create()
        doc = self.document()
        doc["cache"] = {"issues": []}
        self.assertInvalid(doc, "a document with a foreign top-level key")

    def test_it_rejects_a_missing_items_map(self) -> None:
        self.create()
        doc = self.document()
        doc.pop("items")
        self.assertInvalid(doc, "a document with no items map")

    def test_it_rejects_a_foreign_schema_version(self) -> None:
        self.create()
        doc = self.document()
        doc["schema"] = 2
        self.assertInvalid(doc, "a schema-2 document")

    def test_it_rejects_a_non_uuid_key(self) -> None:
        """The O-C lesson: the key must be unique by construction, so a
        short or constant key is not merely unusual, it is invalid."""
        item = self.local_item()
        doc = self.wrap(item)
        doc["items"] = {"_project": item}
        self.assertInvalid(doc, "a constant key")

    def test_it_rejects_a_uuid1_id(self) -> None:
        item = self.local_item()
        item["id"] = str(uuid.uuid1())
        self.assertInvalid(self.wrap(item), "a v1 uuid")

    def test_it_rejects_in_progress_as_a_stored_status(self) -> None:
        item = self.local_item()
        item["status"] = "in_progress"
        self.assertInvalid(self.wrap(item), "a stored in_progress")

    def test_it_rejects_a_local_item_with_no_status(self) -> None:
        item = self.local_item()
        item.pop("status")
        self.assertInvalid(self.wrap(item), "a local item with no status")

    def test_it_rejects_an_invented_state_reason(self) -> None:
        item = self.local_item()
        item["state_reason"] = "wontfix"
        self.assertInvalid(self.wrap(item), "a state_reason GitHub does not have")

    def test_it_rejects_an_unknown_source_kind(self) -> None:
        item = self.local_item()
        item["source"] = {"kind": "gitlab"}
        self.assertInvalid(self.wrap(item), "an unknown source kind")

    def test_it_rejects_a_github_item_with_no_github_block(self) -> None:
        item = self.github_item()
        item["source"].pop("github")
        self.assertInvalid(self.wrap(item), "an adopted item with no issue reference")

    def test_it_rejects_a_local_item_carrying_a_github_block(self) -> None:
        item = self.local_item()
        item["source"]["github"] = dict(GITHUB_REF)
        self.assertInvalid(self.wrap(item), "a local item with an issue reference")

    def test_it_rejects_a_github_block_with_no_node_id(self) -> None:
        """``node_id`` is what survives a repo rename; ``repo``/``number``
        do not, so it is required, not optional."""
        item = self.github_item()
        item["source"]["github"].pop("node_id")
        self.assertInvalid(self.wrap(item), "an issue reference with no node_id")

    def test_it_rejects_a_stringly_typed_issue_number(self) -> None:
        item = self.github_item()
        item["source"]["github"]["number"] = "42"
        self.assertInvalid(self.wrap(item), "a string issue number")

    def test_it_rejects_a_snapshotted_status_on_an_adopted_item(self) -> None:
        """§5.3, the subtle half: a stale ``closed`` is a false statement
        about who owes what. Absent beats wrong."""
        for field, value in (
            ("status", "closed"),
            ("state_reason", "completed"),
            ("assignee", "someone-else"),
            ("body", "the issue text"),
        ):
            with self.subTest(field=field):
                item = self.github_item()
                item[field] = value
                self.assertInvalid(self.wrap(item), f"an adopted item carrying {field}")

    def test_it_rejects_an_undeclared_field_on_a_record(self) -> None:
        item = self.local_item()
        item["priority"] = "p0"
        self.assertInvalid(self.wrap(item), "a record with a foreign field")

    def test_it_rejects_a_record_missing_its_links(self) -> None:
        item = self.local_item()
        item.pop("links")
        self.assertInvalid(self.wrap(item), "a record with no links block")

    def test_it_rejects_links_of_the_wrong_shape(self) -> None:
        item = self.local_item()
        item["links"]["todo_ids"] = "abc12345"
        self.assertInvalid(self.wrap(item), "a scalar todo_ids")

    def test_it_rejects_an_empty_title(self) -> None:
        item = self.local_item()
        item["title"] = ""
        self.assertInvalid(self.wrap(item), "an empty title")

    def test_it_rejects_a_non_string_label(self) -> None:
        item = self.local_item()
        item["labels"] = [{"name": "infra"}]
        self.assertInvalid(self.wrap(item), "an object label")

    def test_it_rejects_a_boolean_tombstone(self) -> None:
        item = self.local_item()
        item["deleted_at"] = True
        self.assertInvalid(self.wrap(item), "a boolean deleted_at")

    def test_it_rejects_a_record_that_is_not_an_object(self) -> None:
        item = self.local_item()
        doc = self.wrap(item)
        doc["items"][item["id"]] = "gone"
        self.assertInvalid(doc, "a string in place of a record")


# ── 2. CRUD ───────────────────────────────────────────────────────────────────


class CreateTests(_WorkitemsCase):
    def test_create_writes_a_schema_1_document(self) -> None:
        item = self.create()
        doc = self.document()
        self.assertEqual(doc["schema"], workitems_store.WORKITEMS_SCHEMA)
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["$schema"], "xo/workitems.schema.json")
        self.assertEqual(list(doc["items"]), [item["id"]])
        self.assertEqual(doc["items"][item["id"]], item)

    def test_the_id_is_a_uuid4_and_is_also_the_key(self) -> None:
        """Unique by construction — the O-C lesson. The rollup unions
        these across projects, so the id may not be a short handle that
        could collide, and the key may not be trusted over the record."""
        item = self.create()
        parsed = uuid.UUID(item["id"])
        self.assertEqual(parsed.version, 4)
        self.assertEqual(str(parsed), item["id"])
        self.assertIn(item["id"], self.document()["items"])

    def test_two_creates_get_distinct_ids_and_both_rows_survive(self) -> None:
        first = self.create(title="one")
        second = self.create(title="two")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(
            [row["title"] for row in workitems_store.list_workitems(self.path)],
            ["one", "two"],
        )

    def test_a_local_item_defaults_to_open_and_unassigned(self) -> None:
        item = self.create()
        self.assertEqual(item["status"], "open")
        self.assertIsNone(item["state_reason"])
        self.assertIsNone(item["assignee"])
        self.assertIsNone(item["body"])
        self.assertEqual(item["labels"], [])
        self.assertEqual(item["links"], {"todo_ids": [], "session_ids": []})
        self.assertEqual(item["source"], {"kind": "local"})
        self.assertEqual(item["created_by"], "codex")
        self.assertIsNone(item["deleted_at"])
        self.assertIsNone(item["deleted_by"])
        self.assertEqual(item["created_at"], item["updated_at"])

    def test_an_adopted_item_omits_every_field_github_owns(self) -> None:
        """§5.3. Not null — absent. A null would still be a claim about
        state; an absent key says "ask GitHub", which is the truth."""
        item = self.adopt()
        for field in workitems_store.GITHUB_OWNED_FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(field, item)
        self.assertEqual(item["source"]["github"], GITHUB_REF)
        self.assertEqual(item["title"], "Rate-limit the GitHub poller")

    def test_adoption_refuses_to_snapshot_a_field_github_owns(self) -> None:
        for field, value in (
            ("status", "closed"),
            ("state_reason", "completed"),
            ("assignee", "peer-login"),
            ("body", "issue text"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(WorkitemsStoreError) as raised:
                    self.adopt(**{field: value})
                self.assertEqual(raised.exception.code, "github_authoritative")
        self.assertFalse(self.path.exists(), "a refused create wrote a document")

    def test_a_missing_file_is_created_rather_than_refused(self) -> None:
        self.assertFalse(self.path.exists())
        self.create()
        self.assertTrue(self.path.is_file())

    def test_validation_rejects_the_obvious_junk(self) -> None:
        cases = [
            ("invalid_runtime", dict(runtime="../../etc/passwd")),
            ("invalid_value", dict(title="   ")),
            ("invalid_value", dict(title="x" * 1001)),
            ("invalid_value", dict(body="x" * 16001)),
            ("invalid_value", dict(labels=["ok", 7])),
            ("invalid_value", dict(labels=["with\nnewline"])),
            ("invalid_status", dict(status="in_progress")),
            ("invalid_status", dict(status="")),
            ("invalid_state_reason", dict(state_reason="wontfix")),
            ("invalid_assignee", dict(assignee="not a login")),
            ("invalid_todo_id", dict(todo_ids=["../escape"])),
            ("invalid_session_id", dict(session_ids=[None])),
            ("invalid_source", dict(source={"kind": "gitlab"})),
            ("invalid_source", dict(source={"kind": "github"})),
            ("invalid_source", dict(
                source={"kind": "local", "github": dict(GITHUB_REF)})),
            ("invalid_source", dict(source={
                "kind": "github",
                "github": dict(GITHUB_REF, repo="not-a-repo")})),
            ("invalid_source", dict(source={
                "kind": "github", "github": dict(GITHUB_REF, number=0)})),
            ("invalid_source", dict(source={
                "kind": "github", "github": dict(GITHUB_REF, number="42")})),
            ("invalid_source", dict(source={
                "kind": "github",
                "github": dict(GITHUB_REF, url="javascript:alert(1)")})),
            ("invalid_source", dict(source={
                "kind": "github",
                "github": {k: v for k, v in GITHUB_REF.items() if k != "node_id"}})),
        ]
        for code, kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(WorkitemsStoreError) as raised:
                    self.create(**kwargs)
                self.assertEqual(raised.exception.code, code)
        self.assertFalse(self.path.exists(), "a rejected create wrote a document")


class ReadTests(_WorkitemsCase):
    def test_get_returns_the_record_and_none_for_a_stranger(self) -> None:
        item = self.create()
        self.assertEqual(workitems_store.get_workitem(self.path, item["id"]), item)
        self.assertIsNone(workitems_store.get_workitem(self.path, str(uuid.uuid4())))

    def test_reading_a_file_that_does_not_exist_is_empty_not_an_error(self) -> None:
        self.assertEqual(workitems_store.list_workitems(self.path), [])
        self.assertIsNone(workitems_store.get_workitem(self.path, str(uuid.uuid4())))
        self.assertFalse(self.path.exists(), "a read created the document")

    def test_list_filters_on_stored_status_and_kind(self) -> None:
        open_item = self.create(title="open one")
        closed_item = self.create(title="closed one", status="closed")
        adopted = self.adopt(title="adopted one")

        def titles(**kwargs) -> list[str]:
            return [row["title"] for row in workitems_store.list_workitems(self.path, **kwargs)]

        self.assertEqual(titles(), ["open one", "closed one", "adopted one"])
        self.assertEqual(titles(status="open"), ["open one"])
        self.assertEqual(titles(status="closed"), ["closed one"])
        self.assertEqual(titles(kind="github"), ["adopted one"])
        self.assertEqual(titles(kind="local"), ["open one", "closed one"])
        self.assertEqual([open_item["id"], closed_item["id"], adopted["id"]],
                         [row["id"] for row in workitems_store.list_workitems(self.path)])

    def test_a_status_filter_never_matches_an_adopted_item(self) -> None:
        """It stores no status, by design — the mirror answers for it
        (§5.3, W7). Answering from the file would mean answering wrong."""
        self.adopt()
        self.assertEqual(workitems_store.list_workitems(self.path, status="open"), [])
        self.assertEqual(workitems_store.list_workitems(self.path, status="closed"), [])
        self.assertEqual(len(workitems_store.list_workitems(self.path)), 1)

    def test_list_filters_on_assignee(self) -> None:
        mine = self.create(title="mine", assignee="dwivedi-ai")
        self.create(title="theirs", assignee="someone-else")
        rows = workitems_store.list_workitems(self.path, assignee="dwivedi-ai")
        self.assertEqual([row["id"] for row in rows], [mine["id"]])

    def test_a_returned_record_is_a_copy(self) -> None:
        """Callers get their own object: mutating a read must not edit
        the store's view of the document."""
        item = self.create()
        fetched = workitems_store.get_workitem(self.path, item["id"])
        fetched["title"] = "mutated"
        self.assertEqual(
            workitems_store.get_workitem(self.path, item["id"])["title"],
            "Rate-limit the GitHub poller",
        )

    def test_list_rejects_a_filter_it_cannot_honour(self) -> None:
        self.create()
        for code, kwargs in (
            ("invalid_status", dict(status="in_progress")),
            ("invalid_source", dict(kind="gitlab")),
            ("invalid_assignee", dict(assignee="not a login")),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(WorkitemsStoreError) as raised:
                    workitems_store.list_workitems(self.path, **kwargs)
                self.assertEqual(raised.exception.code, code)


class UpdateTests(_WorkitemsCase):
    def test_update_changes_fields_and_stamps_updated_at(self) -> None:
        item = self.create()
        with patch.object(workitems_store, "_now_iso", return_value="2030-01-01T00:00:00Z"):
            updated = workitems_store.update_workitem(
                self.path, item["id"], title="new", status="closed",
                state_reason="completed", labels=["infra"],
                todo_ids=["abc12345"], session_ids=["s1"],
            )
        self.assertEqual(updated["title"], "new")
        self.assertEqual(updated["status"], "closed")
        self.assertEqual(updated["state_reason"], "completed")
        self.assertEqual(updated["labels"], ["infra"])
        self.assertEqual(updated["links"],
                         {"todo_ids": ["abc12345"], "session_ids": ["s1"]})
        self.assertEqual(updated["updated_at"], "2030-01-01T00:00:00Z")
        self.assertEqual(updated["created_at"], item["created_at"])
        self.assertEqual(self.document()["items"][item["id"]], updated)

    def test_none_clears_a_nullable_field_and_is_not_read_as_absent(self) -> None:
        """``assignee=None`` un-assigns; omitting it leaves it alone. A
        PATCH that could not tell those apart could never un-assign."""
        item = self.create(assignee="dwivedi-ai", body="text", state_reason=None)
        untouched = workitems_store.update_workitem(self.path, item["id"], title="new")
        self.assertEqual(untouched["assignee"], "dwivedi-ai")
        cleared = workitems_store.update_workitem(
            self.path, item["id"], assignee=None, body=None,
        )
        self.assertIsNone(cleared["assignee"])
        self.assertIsNone(cleared["body"])

    def test_passing_the_sentinel_explicitly_is_the_same_as_omitting(self) -> None:
        """``UNSET`` is exported because the route layer (W3) forwards
        "the client did not mention this field" verbatim, and must be
        able to say that without meaning "set it to null"."""
        item = self.create(assignee="dwivedi-ai", body="text")
        before = self.path.read_bytes()
        same = workitems_store.update_workitem(
            self.path, item["id"], body=UNSET, assignee=UNSET, state_reason=UNSET,
        )
        self.assertEqual(same, item)
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_no_op_update_writes_nothing_at_all(self) -> None:
        item = self.create()
        before = self.path.read_bytes()
        same = workitems_store.update_workitem(
            self.path, item["id"], title=item["title"], status="open",
        )
        self.assertEqual(same, item)
        self.assertEqual(self.path.read_bytes(), before)

    def test_update_refuses_a_field_github_owns(self) -> None:
        item = self.adopt()
        for field, value in (
            ("status", "closed"),
            ("state_reason", "completed"),
            ("assignee", "peer-login"),
            ("body", "text"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(WorkitemsStoreError) as raised:
                    workitems_store.update_workitem(
                        self.path, item["id"], **{field: value}
                    )
                self.assertEqual(raised.exception.code, "github_authoritative")
        self.assertEqual(self.document()["items"][item["id"]], item)

    def test_update_still_edits_the_local_half_of_an_adopted_item(self) -> None:
        item = self.adopt()
        updated = workitems_store.update_workitem(
            self.path, item["id"], title="renamed locally",
            labels=["infra"], todo_ids=["abc12345"],
        )
        self.assertEqual(updated["title"], "renamed locally")
        self.assertEqual(updated["labels"], ["infra"])
        self.assertEqual(updated["links"]["todo_ids"], ["abc12345"])
        for field in workitems_store.GITHUB_OWNED_FIELDS:
            self.assertNotIn(field, updated)

    def test_update_rejects_an_unknown_id(self) -> None:
        with self.assertRaises(WorkitemsStoreError) as raised:
            workitems_store.update_workitem(self.path, str(uuid.uuid4()), title="x")
        self.assertEqual(raised.exception.code, "workitem_not_found")

    def test_update_validates_before_it_touches_the_document(self) -> None:
        item = self.create()
        before = self.path.read_bytes()
        for code, kwargs in (
            ("invalid_status", dict(status="in_progress")),
            ("invalid_status", dict(status="")),
            ("invalid_state_reason", dict(state_reason="wontfix")),
            ("invalid_value", dict(title="")),
            ("invalid_value", dict(labels=["x" * 101])),
            ("invalid_assignee", dict(assignee="not a login")),
            ("invalid_todo_id", dict(todo_ids=["../escape"])),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(WorkitemsStoreError) as raised:
                    workitems_store.update_workitem(self.path, item["id"], **kwargs)
                self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.path.read_bytes(), before)


class DeleteTests(_WorkitemsCase):
    def test_delete_tombstones_rather_than_removing(self) -> None:
        item = self.create()
        self.assertTrue(
            workitems_store.delete_workitem(self.path, item["id"], deleted_by="codex")
        )
        stored = self.document()["items"][item["id"]]
        self.assertIsNotNone(stored["deleted_at"])
        self.assertEqual(stored["deleted_by"], "codex")
        self.assertEqual(stored["status"], "open", "the tombstone rewrote the status")

    def test_a_tombstone_is_hidden_from_reads_unless_asked_for(self) -> None:
        alive = self.create(title="alive")
        gone = self.create(title="gone")
        workitems_store.delete_workitem(self.path, gone["id"])

        self.assertIsNone(workitems_store.get_workitem(self.path, gone["id"]))
        self.assertIsNotNone(
            workitems_store.get_workitem(self.path, gone["id"], include_deleted=True)
        )
        self.assertEqual(
            [row["id"] for row in workitems_store.list_workitems(self.path)],
            [alive["id"]],
        )
        self.assertEqual(
            sorted(row["id"] for row in workitems_store.list_workitems(
                self.path, include_deleted=True)),
            sorted([alive["id"], gone["id"]]),
        )

    def test_delete_is_idempotent_rather_than_an_error(self) -> None:
        item = self.create()
        self.assertTrue(workitems_store.delete_workitem(self.path, item["id"]))
        self.assertFalse(workitems_store.delete_workitem(self.path, item["id"]))
        self.assertFalse(workitems_store.delete_workitem(self.path, str(uuid.uuid4())))

    def test_a_deleted_workitem_cannot_be_edited_back_to_life(self) -> None:
        item = self.create()
        workitems_store.delete_workitem(self.path, item["id"])
        with self.assertRaises(WorkitemsStoreError) as raised:
            workitems_store.update_workitem(self.path, item["id"], status="closed")
        self.assertEqual(raised.exception.code, "workitem_not_found")
        self.assertEqual(self.document()["items"][item["id"]]["status"], "open")

    def test_deleted_by_is_held_to_the_runtime_vocabulary(self) -> None:
        """It is persisted into a synced document, so it may not become a
        channel for arbitrary caller text."""
        item = self.create()
        with self.assertRaises(WorkitemsStoreError) as raised:
            workitems_store.delete_workitem(
                self.path, item["id"], deleted_by="../../etc/passwd"
            )
        self.assertEqual(raised.exception.code, "invalid_runtime")
        self.assertIsNone(self.document()["items"][item["id"]]["deleted_at"])


class ConcurrencyTests(_WorkitemsCase):
    def test_concurrent_creates_keep_every_row(self) -> None:
        """``flock`` + read-modify-write under the lock. Without it the
        last writer's document wins and the other rows are gone."""
        errors: list[BaseException] = []

        def make(n: int) -> None:
            try:
                self.create(title=f"item {n}")
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                errors.append(exc)

        threads = [threading.Thread(target=make, args=(n,)) for n in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        titles = sorted(
            row["title"] for row in workitems_store.list_workitems(self.path)
        )
        self.assertEqual(titles, sorted(f"item {n}" for n in range(12)))

    def test_concurrent_updates_and_deletes_keep_every_row(self) -> None:
        items = [self.create(title=f"item {n}") for n in range(8)]

        def edit(index: int) -> None:
            item = items[index]
            if index % 2:
                workitems_store.update_workitem(
                    self.path, item["id"], title=f"edited {index}"
                )
            else:
                workitems_store.delete_workitem(self.path, item["id"], deleted_by="codex")

        threads = [threading.Thread(target=edit, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        stored = self.document()["items"]
        self.assertEqual(len(stored), 8)
        for index, item in enumerate(items):
            row = stored[item["id"]]
            if index % 2:
                self.assertEqual(row["title"], f"edited {index}")
                self.assertIsNone(row["deleted_at"])
            else:
                self.assertIsNotNone(row["deleted_at"])


# ── 3. the O-E refusal ────────────────────────────────────────────────────────


class CorruptDocumentTests(_WorkitemsCase):
    """The one hard requirement of W2.

    ``todos_store._read_sessions`` maps a corrupt ``todos.json`` to
    ``(None, {})``, so the next create writes a document holding one
    todo and silently discards everything the file held — O-E. This
    store must raise and refuse instead, at every entry point, and must
    leave the unreadable bytes exactly where it found them so a human
    can still recover them.
    """

    #: Every way a ``workitems.json`` can be present and unusable.
    CORRUPTIONS: tuple[tuple[str, str], ...] = (
        ("truncated JSON", '{"schema": 1, "items": {"8f3a1c92-6b0e-4c11-9a3d-5e2f'),
        ("not JSON at all", "<<<<<<< HEAD\nmerge conflict\n"),
        ("empty file", ""),
        ("whitespace only", "   \n"),
        ("a JSON list", "[]"),
        ("a JSON scalar", '"workitems"'),
        ("no items map", '{"schema": 1}'),
        ("items is a list", '{"schema": 1, "items": []}'),
        ("a record that is not an object", json.dumps(
            {"schema": 1, "items": {"8f3a1c92-6b0e-4c11-9a3d-5e2f7c1d0a44": "gone"}})),
        ("a key that is not a uuid", json.dumps(
            {"schema": 1, "items": {"_project": {"id": "_project"}}})),
        ("a key that disagrees with its record", json.dumps(
            {"schema": 1, "items": {
                "8f3a1c92-6b0e-4c11-9a3d-5e2f7c1d0a44": {
                    "id": "11111111-1111-4111-8111-111111111111"}}})),
    )

    def _corrupt_file(self, text: str) -> bytes:
        self.path.write_text(text, encoding="utf-8")
        return self.path.read_bytes()

    def test_every_corruption_is_refused_by_every_entry_point(self) -> None:
        wid = "8f3a1c92-6b0e-4c11-9a3d-5e2f7c1d0a44"
        calls = {
            "create": lambda: self.create(),
            "get": lambda: workitems_store.get_workitem(self.path, wid),
            "list": lambda: workitems_store.list_workitems(self.path),
            "update": lambda: workitems_store.update_workitem(
                self.path, wid, title="x"),
            "delete": lambda: workitems_store.delete_workitem(self.path, wid),
        }
        for label, text in self.CORRUPTIONS:
            for name, call in calls.items():
                with self.subTest(corruption=label, call=name):
                    before = self._corrupt_file(text)
                    with self.assertRaises(WorkitemsStoreError) as raised:
                        call()
                    self.assertEqual(raised.exception.code, "corrupt_document")
                    self.assertEqual(
                        self.path.read_bytes(), before,
                        "the store rewrote a document it could not read",
                    )

    def test_the_o_e_failure_mode_is_not_reproduced(self) -> None:
        """The defect, spelled out: a file holding real work, corrupted,
        must not be replaced by a document holding one fresh workitem."""
        self.create(title="work someone did")
        self.create(title="more work someone did")
        good = self.path.read_text(encoding="utf-8")
        damaged = good[: len(good) // 2]
        self.path.write_text(damaged, encoding="utf-8")

        with self.assertRaises(WorkitemsStoreError) as raised:
            self.create(title="the write that would have eaten it")

        self.assertEqual(raised.exception.code, "corrupt_document")
        self.assertEqual(self.path.read_text(encoding="utf-8"), damaged)
        self.assertIn("O-E", raised.exception.message)

        # And once a human repairs the file, the store carries on.
        self.path.write_text(good, encoding="utf-8")
        self.create(title="after the repair")
        self.assertEqual(
            [row["title"] for row in workitems_store.list_workitems(self.path)],
            ["work someone did", "more work someone did", "after the repair"],
        )

    def test_a_newer_schema_is_refused_rather_than_downgraded(self) -> None:
        """A document restored from a Space running a later revision
        carries keys this one would drop on the next write. Dropping them
        silently is the same defect wearing a different hat."""
        self.path.write_text(
            json.dumps({"schema": 2, "items": {}}), encoding="utf-8"
        )
        before = self.path.read_bytes()
        with self.assertRaises(WorkitemsStoreError) as raised:
            self.create()
        self.assertEqual(raised.exception.code, "unsupported_schema")
        self.assertEqual(self.path.read_bytes(), before)

    def test_an_absent_file_is_still_the_one_thing_that_reads_as_empty(self) -> None:
        """The refusal must not swallow the legitimate empty case, or the
        very first create in a project would fail."""
        self.assertFalse(self.path.exists())
        self.assertEqual(workitems_store.list_workitems(self.path), [])
        self.assertIsNotNone(self.create())

    def test_a_document_gone_corrupt_under_the_lock_is_still_refused(self) -> None:
        """Belt and braces: ``_read_document`` refuses first, and the
        write goes through ``write_json_owned``, whose own
        ``CorruptDocumentError`` is translated to the same store error —
        so the file is guarded by the shared machinery as well as by this
        module's read."""
        self.create()
        real_read = workitems_store._read_document

        def read_then_corrupt(path: Path):
            result = real_read(path)
            path.write_text("{ truncated", encoding="utf-8")
            return result

        with patch.object(workitems_store, "_read_document", read_then_corrupt):
            with self.assertRaises(WorkitemsStoreError) as raised:
                self.create(title="second")
        self.assertEqual(raised.exception.code, "corrupt_document")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ truncated")


if __name__ == "__main__":
    unittest.main()
