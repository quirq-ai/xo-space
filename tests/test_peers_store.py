"""Peers: the roster, the store, and the two decisions it had to make.

``<project>/.xo/peers.json`` shipped in the project template with a
schema already written and **no writer anywhere in the tree**. This
suite covers the writer, in ascending order of how much each group
would hurt if it broke:

* **the schema rejects, and the shipped stub still validates** — a
  schema that accepts everything is worse than one that is stale, so
  every "must not validate" case below is doing the real work. The
  positive cases are fed from the store's own output, so the record and
  its validator cannot drift; and the template stub is checked
  separately, because it is the one document every scaffolded project
  starts from.

* **the identity charset really is the assignee charset** — the whole
  point of a roster is that a listed peer can be given work, so a
  ``user_id`` that this store accepts must always be a ``user_id``
  ``workitems_store`` accepts as an ``assignee``. Pinned twice: the
  patterns are compared as strings (they are duplicated by this tree's
  convention, and a duplicate can drift) and then *behaviourally*, by
  feeding the same ids to both stores.

* **the store's CRUD** — a hard delete instead of a tombstone, a POST
  that conflicts instead of upserting, an immutable ``user_id``, and an
  ``updated_at`` that moves only when something actually changed.

* **a corrupt document raises** — the O-E defect
  (``docs/OUTSTANDING.md``), where ``todos_store`` maps an unparseable
  file to "no sessions" and the next create silently discards what the
  file held. Every entry point is checked, and the bytes on disk are
  asserted unchanged afterwards: refusing to write is only half of it,
  the other half is not damaging the evidence. It matters more for this
  document than for either sibling — a roster read as empty is a project
  that has quietly forgotten every collaborator.

Never touches the real ``~/xo-projects`` or ``~/.quirq``: both roots are
redirected into a temp dir and the peers file is addressed by path.
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

from services.cowork_agent.visualizer import (
    peers_store,
    todos_store,
    workitems_store,
)
from services.cowork_agent.visualizer.peers_store import UNSET, PeersStoreError


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    ROOT / "services" / "cowork_agent" / "visualizer" / "schema" / "peers.schema.json"
)
TEMPLATE_STUB = (
    ROOT / "services" / "cowork_agent" / "project_template" / ".xo" / "peers.json"
)

_SKIP_REASON = (
    "jsonschema is not installed — "
    "uv pip install --python venv/bin/python -r requirements-dev.txt"
)


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class _PeersCase(unittest.TestCase):
    """Temp project + redirected roots, shared by every case below."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.xo_dir = tmp / "xo-projects" / "demo" / ".xo"
        self.xo_dir.mkdir(parents=True)
        self.path = self.xo_dir / "peers.json"
        self.workitems_path = self.xo_dir / "workitems.json"
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
        kwargs.setdefault("user_id", "ada")
        kwargs.setdefault("role", "collaborator")
        return peers_store.create_peer(self.path, **kwargs)

    def ids(self) -> list[str]:
        return [row["user_id"] for row in peers_store.list_peers(self.path)]


# ── 1. the schema ─────────────────────────────────────────────────────────────


@unittest.skipIf(Draft7Validator is None, _SKIP_REASON)
class SchemaFileTests(unittest.TestCase):
    """The file itself, and the conventions its twelve siblings follow."""

    def test_it_is_a_valid_draft7_schema(self) -> None:
        Draft7Validator.check_schema(_schema())

    def test_it_declares_draft7_and_the_id_convention(self) -> None:
        schema = _schema()
        self.assertEqual(
            schema["$schema"], "http://json-schema.org/draft-07/schema#"
        )
        self.assertEqual(schema["$id"], "xo/peers.schema.json")

    def test_the_store_stamps_the_schemas_own_id(self) -> None:
        """The ``$schema`` a document carries is the schema's ``$id`` — a
        logical id, never a path into a ``.xo/schema/`` directory that has
        never existed (syncplan T16)."""
        self.assertEqual(peers_store._SCHEMA_REF, _schema()["$id"])

    def test_the_declared_version_matches_the_store(self) -> None:
        self.assertEqual(
            _schema()["properties"]["schema"]["const"], peers_store.PEERS_SCHEMA
        )

    def test_the_role_enum_is_the_stores_vocabulary(self) -> None:
        enum = _schema()["properties"]["peers"]["items"]["properties"]["role"]["enum"]
        self.assertEqual(set(enum), set(peers_store.VALID_ROLES))

    def test_the_store_owns_exactly_the_top_level_keys_the_schema_declares(
        self,
    ) -> None:
        """``additionalProperties: false`` and a single writer: the
        ownership set handed to ``write_json_owned`` is the whole
        document, and a key the schema adds later that nothing declares
        would be silently dropped on the next write."""
        self.assertEqual(
            peers_store._OWNS, frozenset(_schema()["properties"])
        )


@unittest.skipIf(Draft7Validator is None, _SKIP_REASON)
class ShippedTemplateTests(unittest.TestCase):
    """The stub every scaffolded project starts from.

    It is not the store's output, so it gets its own check: a template
    that stopped validating would put an invalid document into every new
    project on day one, and a template the store refuses to read would
    make the very first ``POST /peers`` a 409.
    """

    def test_the_shipped_stub_validates(self) -> None:
        stub = json.loads(TEMPLATE_STUB.read_text(encoding="utf-8"))
        errors = sorted(
            Draft7Validator(_schema()).iter_errors(stub),
            key=lambda e: list(e.path),
        )
        self.assertEqual(errors, [], "the shipped peers.json stub is invalid")

    def test_the_store_reads_the_shipped_stub_as_an_empty_roster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "peers.json"
            path.write_bytes(TEMPLATE_STUB.read_bytes())
            self.assertEqual(peers_store.read_roster(path), (None, []))

    def test_a_present_but_empty_roster_is_not_a_corrupt_one(self) -> None:
        """"Empty list = solo project" is what the schema's own
        description says, so it must read as an answer rather than as a
        fault — the corrupt-document rule must not swallow it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "peers.json"
            path.write_bytes(TEMPLATE_STUB.read_bytes())
            self.assertEqual(peers_store.list_peers(path), [])
            self.assertIsNone(peers_store.get_peer(path, "ada"))


@unittest.skipIf(Draft7Validator is None, _SKIP_REASON)
class SchemaAcceptsRealOutputTests(_PeersCase):
    """The positive cases are the store's actual bytes, not a fixture."""

    def assertValid(self, document: object, label: str) -> None:
        errors = sorted(
            Draft7Validator(_schema()).iter_errors(document),
            key=lambda e: list(e.path),
        )
        if errors:
            detail = "\n".join(
                f"  at /{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                for e in errors
            )
            self.fail(f"{label} fails peers.schema.json:\n{detail}")

    def test_a_freshly_written_document_validates(self) -> None:
        self.create(user_id="ada", role="owner", label="Ada Lovelace")
        self.assertValid(self.document(), "a document with one peer")

    def test_every_optional_field_validates_both_ways(self) -> None:
        self.create(user_id="ada", role="owner")
        self.create(
            user_id="grace",
            role="viewer",
            label="Grace",
            endpoint="https://space.example/sync",
        )
        self.assertValid(self.document(), "a document with set and unset optionals")

    def test_an_edited_and_pruned_document_still_validates(self) -> None:
        self.create(user_id="ada", role="owner")
        self.create(user_id="grace", role="collaborator")
        peers_store.update_peer(self.path, "grace", role="viewer", label="Grace")
        peers_store.delete_peer(self.path, "ada")
        self.assertValid(self.document(), "a document after a PATCH and a DELETE")


@unittest.skipIf(Draft7Validator is None, _SKIP_REASON)
class SchemaRejectsTests(unittest.TestCase):
    """The half that does the work: what must **not** validate."""

    def wrap(self, peer: dict) -> dict:
        return {"schema": 1, "updated_at": None, "peers": [peer]}

    def assertInvalid(self, document: object, label: str) -> None:
        if Draft7Validator(_schema()).is_valid(document):
            self.fail(f"{label} was accepted by peers.schema.json but must not be")

    def test_a_peer_without_an_identity_is_rejected(self) -> None:
        self.assertInvalid(
            self.wrap({"role": "owner", "added_at": "2026-09-08T10:00:00Z"}),
            "a peer with no user_id",
        )

    def test_a_peer_without_a_role_or_an_added_at_is_rejected(self) -> None:
        self.assertInvalid(
            self.wrap({"user_id": "ada", "added_at": "2026-09-08T10:00:00Z"}),
            "a peer with no role",
        )
        self.assertInvalid(
            self.wrap({"user_id": "ada", "role": "owner"}),
            "a peer with no added_at",
        )

    def test_an_unknown_role_is_rejected(self) -> None:
        self.assertInvalid(
            self.wrap({
                "user_id": "ada", "role": "admin",
                "added_at": "2026-09-08T10:00:00Z",
            }),
            "role: admin",
        )

    def test_a_tombstone_is_rejected(self) -> None:
        """The reason removal is a hard delete, stated as a schema fact:
        ``additionalProperties: false`` means there is nowhere to write
        one without changing a document that already ships."""
        self.assertInvalid(
            self.wrap({
                "user_id": "ada", "role": "owner",
                "added_at": "2026-09-08T10:00:00Z",
                "deleted_at": "2026-09-08T11:00:00Z",
            }),
            "a peer carrying deleted_at",
        )

    def test_a_document_without_peers_is_rejected(self) -> None:
        self.assertInvalid({"schema": 1, "updated_at": None}, "no peers key")

    def test_a_wrong_schema_version_is_rejected(self) -> None:
        self.assertInvalid(
            {"schema": 2, "updated_at": None, "peers": []}, "schema 2"
        )


# ── 2. the identity vocabulary ────────────────────────────────────────────────


class IdentityCharsetTests(_PeersCase):
    """A listed peer must always be assignable.

    That is the entire justification for keeping a roster next to the
    workitems, so it is pinned rather than assumed — twice, because the
    two ways it can break are different. The pattern comparison catches a
    silent edit to either copy; the round-trip catches a divergence that
    is not in the regex at all (a length clamp, an extra check, a
    normalisation step).
    """

    #: Ids that must be legal on both surfaces, and ids that must be
    #: illegal on both. The second list is where a divergence usually
    #: shows up first: a peer store that quietly accepted a space or a
    #: slash would mint a ``user_id`` that could never be assigned.
    LEGAL = (
        "ada",
        "ankitdwivedi",
        "dwivedi-ai",
        "user_42",
        "ankitdwivedi:collabse_07c611",
        "first.last",
        "A" * 200,
    )
    ILLEGAL = (
        "",
        "   ",
        "not a login",
        "../etc/passwd",
        "ada@example.com",
        "ada/grace",
        "A" * 201,
        "ada\nbob",
    )

    def test_the_pattern_is_the_one_the_other_stores_use(self) -> None:
        self.assertEqual(
            peers_store._SAFE_KEY_RE.pattern,
            workitems_store._SAFE_KEY_RE.pattern,
            "a peer user_id and a workitem assignee no longer share a "
            "charset, so a listed peer may not be assignable",
        )
        self.assertEqual(
            peers_store._SAFE_KEY_RE.pattern, todos_store._SAFE_KEY_RE.pattern
        )

    def test_every_legal_peer_id_is_a_legal_assignee(self) -> None:
        for user_id in self.LEGAL:
            with self.subTest(user_id=user_id):
                peer = peers_store.create_peer(
                    self.path, user_id=user_id, role="collaborator"
                )
                self.assertEqual(peer["user_id"], user_id)
                item = workitems_store.create_workitem(
                    self.workitems_path,
                    runtime="codex",
                    title="work for a listed peer",
                    assignee=user_id,
                )
                self.assertEqual(item["assignee"], user_id)

    def test_every_illegal_peer_id_is_also_an_illegal_assignee(self) -> None:
        """The other direction. A peer store that was *more* permissive
        would mint identities the workitems surface could never use."""
        for user_id in self.ILLEGAL:
            with self.subTest(user_id=user_id):
                with self.assertRaises(PeersStoreError) as peer_error:
                    peers_store.create_peer(
                        self.path, user_id=user_id, role="collaborator"
                    )
                self.assertEqual(peer_error.exception.code, "invalid_user_id")
                with self.assertRaises(workitems_store.WorkitemsStoreError):
                    workitems_store.create_workitem(
                        self.workitems_path,
                        runtime="codex",
                        title="t",
                        assignee=user_id,
                    )

    def test_an_assignee_filter_finds_the_work_of_a_listed_peer(self) -> None:
        """End to end rather than by construction: the id a roster hands
        out is the id the workitems query answers to."""
        peer = self.create(user_id="ankitdwivedi:collabse_07c611", role="owner")
        workitems_store.create_workitem(
            self.workitems_path,
            runtime="codex",
            title="assigned work",
            assignee=peer["user_id"],
        )
        found = workitems_store.list_workitems(
            self.workitems_path, assignee=peer["user_id"]
        )
        self.assertEqual([row["title"] for row in found], ["assigned work"])


# ── 3. the store ──────────────────────────────────────────────────────────────


class CreateTests(_PeersCase):
    def test_a_created_peer_carries_the_declared_shape(self) -> None:
        peer = self.create(
            user_id="ada", role="owner", label="Ada", endpoint="https://s.example/x"
        )
        self.assertEqual(
            list(peer), ["user_id", "role", "added_at", "endpoint", "label"]
        )
        self.assertEqual(peer["user_id"], "ada")
        self.assertEqual(peer["role"], "owner")
        self.assertEqual(peer["label"], "Ada")
        self.assertEqual(peer["endpoint"], "https://s.example/x")
        self.assertTrue(peer["added_at"].endswith("Z"))

    def test_optional_fields_default_to_null_rather_than_absent(self) -> None:
        """``null`` and absent both validate, but a record that always
        carries the key is one a reader never has to ``.get()`` around."""
        peer = self.create(user_id="grace", role="viewer")
        self.assertIsNone(peer["label"])
        self.assertIsNone(peer["endpoint"])

    def test_added_at_is_server_set_and_not_a_parameter(self) -> None:
        with self.assertRaises(TypeError):
            peers_store.create_peer(
                self.path, user_id="ada", role="owner",
                added_at="1999-01-01T00:00:00Z",
            )

    def test_the_document_carries_the_schema_ref_and_version(self) -> None:
        self.create()
        doc = self.document()
        self.assertEqual(doc["$schema"], "xo/peers.schema.json")
        self.assertEqual(doc["schema"], peers_store.PEERS_SCHEMA)
        self.assertTrue(doc["updated_at"])

    def test_a_duplicate_user_id_conflicts_rather_than_upserting(self) -> None:
        """The decision, asserted: the roster is a set keyed by identity,
        so a second create is a conflict and **must not** rewrite the
        role. An upsert here would make a privilege change the side
        effect of an insert the caller believed was new."""
        self.create(user_id="ada", role="viewer", label="Ada")
        with self.assertRaises(PeersStoreError) as raised:
            self.create(user_id="ada", role="owner", label="Someone else")
        self.assertEqual(raised.exception.code, "peer_exists")

        # Nothing moved: not the role, not the label, not the count.
        stored = peers_store.get_peer(self.path, "ada")
        self.assertEqual(stored["role"], "viewer")
        self.assertEqual(stored["label"], "Ada")
        self.assertEqual(len(peers_store.list_peers(self.path)), 1)

    def test_the_conflict_message_names_the_edit_that_would_work(self) -> None:
        self.create(user_id="ada", role="viewer")
        with self.assertRaises(PeersStoreError) as raised:
            self.create(user_id="ada", role="owner")
        self.assertIn("PATCH", raised.exception.message)

    def test_validation_refuses_before_anything_is_written(self) -> None:
        for kwargs, code in (
            ({"user_id": "not a login", "role": "owner"}, "invalid_user_id"),
            ({"user_id": "ada", "role": "admin"}, "invalid_role"),
            ({"user_id": "ada", "role": "owner", "label": "x\x00y"}, "invalid_label"),
            ({"user_id": "ada", "role": "owner", "label": ""}, "invalid_label"),
            ({"user_id": "ada", "role": "owner", "endpoint": "space.example"},
             "invalid_endpoint"),
            ({"user_id": "ada", "role": "owner", "endpoint": "ftp://x/y"},
             "invalid_endpoint"),
            ({"user_id": "ada", "role": "owner", "endpoint": "https://a b"},
             "invalid_endpoint"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(PeersStoreError) as raised:
                    peers_store.create_peer(self.path, **kwargs)
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(
                    self.path.exists(),
                    "a rejected create still touched the document",
                )

    def test_an_http_endpoint_is_allowed(self) -> None:
        """A Space on a private network is a real deployment."""
        peer = self.create(endpoint="http://10.0.0.4:5002/sync")
        self.assertEqual(peer["endpoint"], "http://10.0.0.4:5002/sync")

    def test_the_roster_has_a_ceiling(self) -> None:
        """A synced document with an unbounded array is an unbounded
        snapshot. The cap is far above any real team, and refusing is
        better than growing without limit."""
        with patch.object(peers_store, "_MAX_PEERS", 2):
            self.create(user_id="a", role="viewer")
            self.create(user_id="b", role="viewer")
            with self.assertRaises(PeersStoreError) as raised:
                self.create(user_id="c", role="viewer")
        self.assertEqual(raised.exception.code, "invalid_value")
        self.assertEqual(self.ids(), ["a", "b"])


class ReadTests(_PeersCase):
    def test_an_absent_document_is_an_empty_roster(self) -> None:
        self.assertFalse(self.path.exists())
        self.assertEqual(peers_store.read_roster(self.path), (None, []))
        self.assertEqual(peers_store.list_peers(self.path), [])
        self.assertIsNone(peers_store.get_peer(self.path, "ada"))

    def test_peers_come_back_in_the_order_they_were_added(self) -> None:
        for user_id in ("ada", "grace", "alan"):
            self.create(user_id=user_id, role="collaborator")
        self.assertEqual(self.ids(), ["ada", "grace", "alan"])

    def test_the_role_filter_narrows_and_validates(self) -> None:
        self.create(user_id="ada", role="owner")
        self.create(user_id="grace", role="viewer")
        self.create(user_id="alan", role="viewer")
        self.assertEqual(
            [r["user_id"] for r in peers_store.list_peers(self.path, role="viewer")],
            ["grace", "alan"],
        )
        with self.assertRaises(PeersStoreError) as raised:
            peers_store.list_peers(self.path, role="admin")
        self.assertEqual(raised.exception.code, "invalid_role")

    def test_read_roster_serves_the_stamp_and_the_rows_from_one_read(self) -> None:
        self.create(user_id="ada", role="owner")
        updated_at, peers = peers_store.read_roster(self.path)
        self.assertEqual(updated_at, self.document()["updated_at"])
        self.assertEqual([p["user_id"] for p in peers], ["ada"])

    def test_a_returned_record_is_a_copy(self) -> None:
        """Mutating what a read handed back must not reach disk."""
        self.create(user_id="ada", role="owner")
        got = peers_store.get_peer(self.path, "ada")
        got["role"] = "viewer"
        self.assertEqual(peers_store.get_peer(self.path, "ada")["role"], "owner")


class UpdateTests(_PeersCase):
    def test_a_role_change_is_recorded(self) -> None:
        self.create(user_id="ada", role="viewer")
        updated = peers_store.update_peer(self.path, "ada", role="owner")
        self.assertEqual(updated["role"], "owner")
        self.assertEqual(peers_store.get_peer(self.path, "ada")["role"], "owner")

    def test_null_clears_a_nullable_field_and_absent_leaves_it(self) -> None:
        """The three-way PATCH, which is the reason ``UNSET`` exists: a
        roster with no way to remove a display name would be a roster
        that could never correct one."""
        self.create(user_id="ada", role="owner", label="Ada", endpoint="https://a/b")

        peers_store.update_peer(self.path, "ada", role="collaborator")
        kept = peers_store.get_peer(self.path, "ada")
        self.assertEqual(kept["label"], "Ada")
        self.assertEqual(kept["endpoint"], "https://a/b")

        cleared = peers_store.update_peer(self.path, "ada", label=None)
        self.assertIsNone(cleared["label"])
        self.assertEqual(cleared["endpoint"], "https://a/b")

    def test_passing_unset_explicitly_is_the_same_as_omitting(self) -> None:
        """The sentinel is public so a caller assembling kwargs can spell
        "not supplied" without special-casing the call itself."""
        self.create(user_id="ada", role="owner", label="Ada")
        peers_store.update_peer(
            self.path, "ada", role="viewer", label=UNSET, endpoint=UNSET
        )
        stored = peers_store.get_peer(self.path, "ada")
        self.assertEqual(stored["role"], "viewer")
        self.assertEqual(stored["label"], "Ada")
        self.assertIsNone(stored["endpoint"])

    def test_user_id_is_not_editable(self) -> None:
        """Immutable by construction: there is no parameter for it, so a
        rename is a delete plus a create rather than a silent re-key."""
        self.create(user_id="ada", role="owner")
        with self.assertRaises(TypeError):
            peers_store.update_peer(self.path, "ada", user_id="grace")
        with self.assertRaises(TypeError):
            peers_store.update_peer(self.path, "ada", added_at="1999-01-01T00:00:00Z")

    def test_an_unknown_peer_is_peer_not_found(self) -> None:
        self.create(user_id="ada", role="owner")
        with self.assertRaises(PeersStoreError) as raised:
            peers_store.update_peer(self.path, "grace", role="owner")
        self.assertEqual(raised.exception.code, "peer_not_found")

    def test_an_idempotent_patch_writes_nothing_at_all(self) -> None:
        """"Accurate ``updated_at``" means it moves when the roster
        changes and not when it is merely touched."""
        self.create(user_id="ada", role="owner", label="Ada")
        before = self.path.read_bytes()
        peers_store.update_peer(self.path, "ada", role="owner", label="Ada")
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_real_change_advances_the_documents_stamp(self) -> None:
        self.create(user_id="ada", role="viewer")
        with patch.object(peers_store, "_now_iso", return_value="2030-01-01T00:00:00Z"):
            peers_store.update_peer(self.path, "ada", role="owner")
        self.assertEqual(self.document()["updated_at"], "2030-01-01T00:00:00Z")

    def test_validation_refuses_before_anything_is_written(self) -> None:
        self.create(user_id="ada", role="owner")
        before = self.path.read_bytes()
        for kwargs, code in (
            ({"role": "admin"}, "invalid_role"),
            ({"label": "x\x7fy"}, "invalid_label"),
            ({"endpoint": "nope"}, "invalid_endpoint"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(PeersStoreError) as raised:
                    peers_store.update_peer(self.path, "ada", **kwargs)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.path.read_bytes(), before)


class DeleteTests(_PeersCase):
    def test_a_delete_removes_the_record_outright(self) -> None:
        """The deliberate divergence from todos and workitems. A removed
        collaborator must leave **nothing** behind: ``peers.json`` is in
        the synced tier, so a tombstone would travel to every Space this
        project ever reaches."""
        self.create(user_id="ada", role="owner")
        self.create(user_id="grace", role="viewer")

        self.assertTrue(peers_store.delete_peer(self.path, "ada"))

        self.assertEqual(self.ids(), ["grace"])
        self.assertIsNone(peers_store.get_peer(self.path, "ada"))
        self.assertNotIn("ada", self.path.read_text(encoding="utf-8"))
        self.assertNotIn("deleted", self.path.read_text(encoding="utf-8"))

    def test_a_delete_is_idempotent_rather_than_a_404(self) -> None:
        self.create(user_id="ada", role="owner")
        self.assertTrue(peers_store.delete_peer(self.path, "ada"))
        self.assertFalse(peers_store.delete_peer(self.path, "ada"))
        self.assertFalse(peers_store.delete_peer(self.path, "never-listed"))

    def test_a_no_op_delete_writes_nothing(self) -> None:
        self.create(user_id="ada", role="owner")
        before = self.path.read_bytes()
        self.assertFalse(peers_store.delete_peer(self.path, "grace"))
        self.assertEqual(self.path.read_bytes(), before)

    def test_there_is_no_deleted_by_to_thread_through(self) -> None:
        """No attribution parameter, because the schema declares no field
        to hold one — the store never accepts a value it cannot store."""
        self.create(user_id="ada", role="owner")
        with self.assertRaises(TypeError):
            peers_store.delete_peer(self.path, "ada", deleted_by="codex")

    def test_a_removed_peer_can_be_added_again(self) -> None:
        """Because the delete is hard, re-adding is a plain create rather
        than an un-delete — and it gets a fresh ``added_at``, which is
        the truth: this is when they joined *this time*."""
        first = self.create(user_id="ada", role="owner")
        peers_store.delete_peer(self.path, "ada")
        with patch.object(peers_store, "_now_iso", return_value="2030-01-01T00:00:00Z"):
            again = self.create(user_id="ada", role="viewer")
        self.assertEqual(again["role"], "viewer")
        self.assertNotEqual(again["added_at"], first["added_at"])


class ConcurrencyTests(_PeersCase):
    def test_concurrent_creates_keep_every_peer(self) -> None:
        """``flock`` + read-modify-write under the lock. Without it the
        last writer's document wins and the other peers are gone — and
        for a roster that is somebody silently losing access."""
        errors: list[BaseException] = []

        def add(n: int) -> None:
            try:
                self.create(user_id=f"peer-{n}", role="collaborator")
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(n,)) for n in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(sorted(self.ids()), sorted(f"peer-{n}" for n in range(12)))

    def test_concurrent_updates_and_deletes_keep_every_other_row(self) -> None:
        for n in range(8):
            self.create(user_id=f"peer-{n}", role="viewer")

        def edit(n: int) -> None:
            if n % 2:
                peers_store.update_peer(self.path, f"peer-{n}", role="owner")
            else:
                peers_store.delete_peer(self.path, f"peer-{n}")

        threads = [threading.Thread(target=edit, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        rows = {row["user_id"]: row for row in peers_store.list_peers(self.path)}
        self.assertEqual(sorted(rows), sorted(f"peer-{n}" for n in range(1, 8, 2)))
        for row in rows.values():
            self.assertEqual(row["role"], "owner")


# ── 4. the O-E refusal ────────────────────────────────────────────────────────


class CorruptDocumentTests(_PeersCase):
    """The hard requirement, and the one with the highest cost here.

    ``todos_store._read_sessions`` maps a corrupt ``todos.json`` to
    ``(None, {})``, so the next create writes a document holding one todo
    and silently discards everything the file held — O-E. This store must
    raise and refuse instead, at **every** entry point, and must leave
    the unreadable bytes exactly where it found them so a human can still
    recover them. A roster read as empty is a project that has forgotten
    every collaborator, and nothing about it looks wrong.
    """

    #: Every way a ``peers.json`` can be present and unusable.
    CORRUPTIONS: tuple[tuple[str, str], ...] = (
        ("truncated JSON", '{"schema": 1, "peers": [{"user_id": "ad'),
        ("not JSON at all", "<<<<<<< HEAD\nmerge conflict\n"),
        ("empty file", ""),
        ("whitespace only", "   \n"),
        ("a JSON list", "[]"),
        ("a JSON scalar", '"peers"'),
        ("no peers key", '{"schema": 1}'),
        ("peers is null", '{"schema": 1, "peers": null}'),
        ("peers is an object", '{"schema": 1, "peers": {}}'),
        ("an entry that is not an object", '{"schema": 1, "peers": ["ada"]}'),
        ("an entry with no user_id", json.dumps(
            {"schema": 1, "peers": [{"role": "owner"}]})),
        ("an entry with an empty user_id", json.dumps(
            {"schema": 1, "peers": [{"user_id": "", "role": "owner"}]})),
        ("an entry whose user_id is a number", json.dumps(
            {"schema": 1, "peers": [{"user_id": 7, "role": "owner"}]})),
        ("the same person listed twice", json.dumps(
            {"schema": 1, "peers": [
                {"user_id": "ada", "role": "owner",
                 "added_at": "2026-09-08T10:00:00Z"},
                {"user_id": "ada", "role": "viewer",
                 "added_at": "2026-09-08T11:00:00Z"},
            ]})),
    )

    def _corrupt_file(self, text: str) -> bytes:
        self.path.write_text(text, encoding="utf-8")
        return self.path.read_bytes()

    def test_every_corruption_is_refused_by_every_entry_point(self) -> None:
        calls = {
            "create": lambda: self.create(user_id="new-peer", role="viewer"),
            "get": lambda: peers_store.get_peer(self.path, "ada"),
            "list": lambda: peers_store.list_peers(self.path),
            "read_roster": lambda: peers_store.read_roster(self.path),
            "update": lambda: peers_store.update_peer(self.path, "ada", role="owner"),
            "delete": lambda: peers_store.delete_peer(self.path, "ada"),
        }
        for label, text in self.CORRUPTIONS:
            for name, call in calls.items():
                with self.subTest(corruption=label, call=name):
                    before = self._corrupt_file(text)
                    with self.assertRaises(PeersStoreError) as raised:
                        call()
                    self.assertEqual(raised.exception.code, "corrupt_document")
                    self.assertEqual(
                        self.path.read_bytes(), before,
                        "the store rewrote a document it could not read",
                    )

    def test_the_o_e_failure_mode_is_not_reproduced(self) -> None:
        """The defect, spelled out for this document: a roster listing
        real collaborators, corrupted, must not be replaced by a document
        listing one fresh peer — which is exactly how a project silently
        loses everyone it was shared with."""
        self.create(user_id="ada", role="owner")
        self.create(user_id="grace", role="collaborator")
        good = self.path.read_text(encoding="utf-8")
        damaged = good[: len(good) // 2]
        self.path.write_text(damaged, encoding="utf-8")

        with self.assertRaises(PeersStoreError) as raised:
            self.create(user_id="alan", role="viewer")

        self.assertEqual(raised.exception.code, "corrupt_document")
        self.assertEqual(self.path.read_text(encoding="utf-8"), damaged)
        self.assertIn("O-E", raised.exception.message)

        # And once a human repairs the file, the store carries on.
        self.path.write_text(good, encoding="utf-8")
        self.create(user_id="alan", role="viewer")
        self.assertEqual(self.ids(), ["ada", "grace", "alan"])

    def test_a_newer_schema_is_refused_rather_than_downgraded(self) -> None:
        """A roster restored from a Space running a later revision may
        carry per-peer keys this one would drop on the next write."""
        self.path.write_text(
            json.dumps({"schema": 2, "peers": []}), encoding="utf-8"
        )
        before = self.path.read_bytes()
        for name, call in (
            ("create", lambda: self.create()),
            ("list", lambda: peers_store.list_peers(self.path)),
            ("delete", lambda: peers_store.delete_peer(self.path, "ada")),
        ):
            with self.subTest(call=name):
                with self.assertRaises(PeersStoreError) as raised:
                    call()
                self.assertEqual(raised.exception.code, "unsupported_schema")
                self.assertEqual(self.path.read_bytes(), before)

    def test_an_absent_file_is_still_the_one_thing_that_reads_as_empty(self) -> None:
        """The refusal must not swallow the legitimate empty case, or the
        very first peer added to a project would fail."""
        self.assertFalse(self.path.exists())
        self.assertEqual(peers_store.list_peers(self.path), [])
        self.assertIsNotNone(self.create())

    def test_a_document_gone_corrupt_under_the_lock_is_still_refused(self) -> None:
        """Belt and braces: ``_read_document`` refuses first, and the
        write goes through ``write_json_owned``, whose own
        ``CorruptDocumentError`` is translated to the same store error —
        so the file is guarded by the shared machinery as well as by this
        module's read."""
        self.create(user_id="ada", role="owner")
        real_read = peers_store._read_document

        def read_then_corrupt(path: Path):
            result = real_read(path)
            path.write_text("{ truncated", encoding="utf-8")
            return result

        with patch.object(peers_store, "_read_document", read_then_corrupt):
            with self.assertRaises(PeersStoreError) as raised:
                self.create(user_id="grace", role="viewer")
        self.assertEqual(raised.exception.code, "corrupt_document")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ truncated")

    def test_a_stored_id_this_revision_would_not_mint_is_still_readable(self) -> None:
        """Reads are looser than writes, on purpose. The schema types
        ``user_id`` as a plain string, so a document a newer Space wrote
        may carry an id outside this revision's charset; refusing the
        whole roster over one such entry would fail closed on a document
        that is perfectly valid."""
        self.path.write_text(json.dumps({
            "schema": 1,
            "peers": [{
                "user_id": "ada@example.com",
                "role": "owner",
                "added_at": "2026-09-08T10:00:00Z",
            }],
        }), encoding="utf-8")
        self.assertEqual(self.ids(), ["ada@example.com"])
        self.assertIsNotNone(peers_store.get_peer(self.path, "ada@example.com"))
        # …and it can still be removed, which is what matters most.
        self.assertTrue(peers_store.delete_peer(self.path, "ada@example.com"))

    def test_an_unknown_per_peer_key_is_carried_forward_not_dropped(self) -> None:
        """The same principle at record level: silently discarding a key
        a newer Space wrote is the ``unsupported_schema`` defect reached
        by another road."""
        self.path.write_text(json.dumps({
            "schema": 1,
            "peers": [{
                "user_id": "ada",
                "role": "owner",
                "added_at": "2026-09-08T10:00:00Z",
                "invited_by": "grace",
            }],
        }), encoding="utf-8")
        peers_store.update_peer(self.path, "ada", role="viewer")
        stored = self.document()["peers"][0]
        self.assertEqual(stored["role"], "viewer")
        self.assertEqual(stored["invited_by"], "grace")


if __name__ == "__main__":
    unittest.main()
