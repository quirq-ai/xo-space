"""``.xo/project.json`` identity fill — the sink merges, it does not rebuild.

Three writers share this one document (docs/syncplan.md §5.1):

* ``visualizer/sinks/project_json.fill_identity`` owns ``schema``, ``pid``,
  ``name``, ``owner_user_id``, ``created_at``, and is the only place that
  drops ``_template``.
* ``project_layout._upsert_metadata`` owns ``display_name`` and
  ``description`` (and seeds ``name``/``created_at`` at scaffold time,
  because it is what creates the file). It must never mint ``pid``.
* the git refresher owns ``git``, the durable provenance block schema 2
  declares (syncplan T12/T13 populate it).

Both live writers go through ``write_json_owned``, so the ownership table
is enforced by the declared ``owns`` set rather than by convention.

Before the fix the sink wrote a fresh five-key literal, so every tick
(once a second) deleted ``display_name``, ``description`` and any manually
curated key such as ``category`` — for every project, forever, because
``_upsert_metadata`` left ``_template: true`` in place and the sink's
"already filled" guard therefore never closed.

The other invariant here: ``pid`` is minted once and never regenerated
(``project.schema.json:14``). A *missing* file is safe to mint into; a
file that exists but will not parse is not — it may still hold the pid, so
the sink no-ops instead of overwriting it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer.sinks import project_json

# The sink's format: no offset, no microseconds.
_TS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class _TempRoot(unittest.TestCase):
    """Every test runs against a throwaway projects root and state root, so
    nothing can touch the developer's real ~/xo-projects or ~/.quirq."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir(parents=True)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir(parents=True)
        env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.root),
                "QUIRQ_STATE_ROOT": str(self.state),
                # Force the bundled template even if the developer's shell
                # points XO_PROJECT_TEMPLATE somewhere else.
                "XO_PROJECT_TEMPLATE": "",
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    # ── helpers ──────────────────────────────────────────────────────────
    def _xo(self, name: str, meta: dict | str | None = None) -> Path:
        """Create ``<root>/<name>/.xo/`` and optionally seed project.json.

        ``meta`` as a ``str`` is written verbatim — used for the corrupt case.
        """
        xo = self.root / name / ".xo"
        xo.mkdir(parents=True, exist_ok=True)
        if meta is not None:
            raw = meta if isinstance(meta, str) else json.dumps(meta, indent=2)
            (xo / "project.json").write_text(raw, encoding="utf-8")
        return xo

    def _read(self, xo: Path) -> dict:
        return json.loads((xo / "project.json").read_text(encoding="utf-8"))


class FillIdentityMergeTests(_TempRoot):
    def test_unknown_key_survives_a_tick(self) -> None:
        """A key no writer here owns — a hand-set category — is carried
        forward. This is the data-loss bug: the sink used to rebuild."""
        xo = self._xo("research-notes", {"_template": True, "category": "research"})

        self.assertTrue(project_json.fill_identity(xo, "research-notes"))

        meta = self._read(xo)
        self.assertEqual(meta["category"], "research")

    def test_display_name_and_description_survive_a_tick(self) -> None:
        """The keys ``_upsert_metadata`` owns are not the sink's to delete."""
        xo = self._xo(
            "blackhole",
            {
                "schema": 1,
                "_template": True,
                "pid": None,
                "name": None,
                "owner_user_id": None,
                "created_at": None,
                "display_name": "Blackhole",
                "description": "Event horizon sim.",
            },
        )

        self.assertTrue(project_json.fill_identity(xo, "blackhole"))

        meta = self._read(xo)
        self.assertEqual(meta["display_name"], "Blackhole")
        self.assertEqual(meta["description"], "Event horizon sim.")

    def test_existing_pid_is_never_regenerated(self) -> None:
        """``pid`` travels with a clone; regenerating it forks the identity."""
        pid = "6f1c0d2e-0000-4000-8000-000000000001"
        xo = self._xo(
            "cloned",
            {"schema": 1, "_template": True, "pid": pid, "name": "origin-name"},
        )

        project_json.fill_identity(xo, "cloned")
        meta = self._read(xo)

        self.assertEqual(meta["pid"], pid)
        # An explicit name is the user's, not the folder's.
        self.assertEqual(meta["name"], "origin-name")

    def test_template_marker_is_removed(self) -> None:
        """The sink is the load-bearing half of dropping ``_template``: as
        long as it is set, the "already filled" guard stays open."""
        xo = self._xo("fresh", {"schema": 1, "_template": True, "pid": None})

        self.assertTrue(project_json.fill_identity(xo, "fresh"))

        self.assertNotIn("_template", self._read(xo))

    def test_second_call_is_a_no_op(self) -> None:
        """Idempotent: the second tick writes nothing and reports it."""
        xo = self._xo("fresh", {"schema": 1, "_template": True, "pid": None})

        self.assertTrue(project_json.fill_identity(xo, "fresh"))
        first = self._read(xo)
        self.assertFalse(project_json.fill_identity(xo, "fresh"))
        self.assertEqual(self._read(xo), first)

    def test_fills_the_five_owned_keys(self) -> None:
        xo = self._xo("nova", {"schema": 1, "_template": True, "pid": None,
                               "name": None, "owner_user_id": None,
                               "created_at": None})

        self.assertTrue(project_json.fill_identity(xo, "nova"))

        meta = self._read(xo)
        self.assertEqual(meta["schema"], 1)
        self.assertTrue(meta["pid"])
        self.assertEqual(meta["name"], "nova")
        self.assertTrue(meta["owner_user_id"])
        self.assertRegex(meta["created_at"], _TS)

    def test_missing_file_is_created(self) -> None:
        """Absent is not the same as corrupt — absent is safe to mint into."""
        xo = self._xo("brand-new")

        self.assertTrue(project_json.fill_identity(xo, "brand-new"))
        self.assertTrue(self._read(xo)["pid"])

    def test_ghost_project_is_not_conjured(self) -> None:
        """No directory, no write: the folder *is* the project."""
        xo = self.root / "not-a-project" / ".xo"

        self.assertFalse(project_json.fill_identity(xo, "not-a-project"))
        self.assertFalse(xo.exists())


class CorruptDocumentTests(_TempRoot):
    def test_corrupt_project_json_does_not_mint_a_new_pid(self) -> None:
        """``read_json`` returns None for "absent" *and* for "unparseable".
        Treating the second as the first mints a fresh pid over a document
        that may still hold the real one."""
        raw = '{"schema": 1, "pid": "6f1c0d2e-0000-4000-8000-0000000000ff",'
        xo = self._xo("corrupt", raw)

        # It also says so, once per path — a silent no-op would look like a
        # project that simply never gets an identity.
        with self.assertLogs(project_json.logger, level="WARNING") as caught:
            self.assertFalse(project_json.fill_identity(xo, "corrupt"))
        self.assertIn("refusing to mint a new pid", caught.output[0])
        # The file is left byte-for-byte alone for a human to repair.
        self.assertEqual((xo / "project.json").read_text(encoding="utf-8"), raw)

    def test_empty_project_json_is_treated_as_corrupt(self) -> None:
        xo = self._xo("emptied", "")

        with self.assertLogs(project_json.logger, level="WARNING"):
            self.assertFalse(project_json.fill_identity(xo, "emptied"))
        self.assertEqual((xo / "project.json").read_text(encoding="utf-8"), "")

    def test_non_object_project_json_is_treated_as_corrupt(self) -> None:
        """Parseable but not a document — ``current.get`` would have raised."""
        xo = self._xo("listy", "[1, 2, 3]")

        with self.assertLogs(project_json.logger, level="WARNING"):
            self.assertFalse(project_json.fill_identity(xo, "listy"))
        self.assertEqual((xo / "project.json").read_text(encoding="utf-8"), "[1, 2, 3]")


class ScaffoldRoundTripTests(_TempRoot):
    """T2: what ``scaffold_project`` leaves on disk must survive the watcher."""

    def test_scaffold_then_tick_keeps_metadata_and_one_timestamp_format(self) -> None:
        meta = project_layout.scaffold_project(
            "blackhole",
            display_name="Blackhole",
            description="Event horizon sim.",
        )
        self.assertNotIn("_template", meta)
        self.assertRegex(meta["created_at"], _TS)
        # Identity is the sink's to mint, never the scaffolder's.
        self.assertIsNone(meta.get("pid"))

        xo = project_layout.xo_dir("blackhole")
        self.assertTrue(project_json.fill_identity(xo, "blackhole"))

        after = self._read(xo)
        self.assertEqual(after["display_name"], "Blackhole")
        self.assertEqual(after["description"], "Event horizon sim.")
        self.assertNotIn("_template", after)
        self.assertRegex(after["created_at"], _TS)
        self.assertTrue(after["pid"])
        self.assertEqual(after["name"], "blackhole")

        # And the next tick changes nothing.
        self.assertFalse(project_json.fill_identity(xo, "blackhole"))
        self.assertEqual(self._read(xo), after)

    def test_scaffold_does_not_null_out_identity_keys(self) -> None:
        """The template ships every identity key present-but-null, so an
        ``in``-only check left ``name``/``created_at`` null after a scaffold."""
        meta = project_layout.scaffold_project("nulls")

        self.assertEqual(meta["name"], "nulls")
        self.assertIsNotNone(meta["created_at"])

    def test_rescaffold_preserves_the_minted_pid(self) -> None:
        """Re-running the scaffolder over a live project (the idempotent
        fill-in path) must not disturb identity."""
        project_layout.scaffold_project("stable", display_name="Stable")
        xo = project_layout.xo_dir("stable")
        project_json.fill_identity(xo, "stable")
        pid = self._read(xo)["pid"]

        project_layout.scaffold_project("stable", description="Now described.")

        after = self._read(xo)
        self.assertEqual(after["pid"], pid)
        self.assertEqual(after["display_name"], "Stable")
        self.assertEqual(after["description"], "Now described.")
        self.assertNotIn("_template", after)


# ── Schema 2 (docs/syncplan.md §5.1) ─────────────────────────────────────────

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "services" / "cowork_agent" / "visualizer" / "schema" / "project.schema.json"
)
_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "services" / "cowork_agent" / "project_template" / ".xo" / "project.json"
)

try:  # pragma: no cover - exercised by whichever branch is installed
    import jsonschema as _jsonschema
except ImportError:  # T16 adds it to requirements-dev; until then, validate here.
    _jsonschema = None

_JSON_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "null": type(None), "number": (int, float), "integer": int,
}


def _errors(doc, schema, where="$"):
    """Validate the subset of draft-07 these schemas actually use.

    ``type`` (single or list), ``enum``, ``const``, ``required``,
    ``properties`` and ``additionalProperties: false``, recursing into
    objects. Deliberately tiny: it exists so the acceptance criterion is
    checkable before ``jsonschema`` is a dependency (syncplan T16), and it
    is bypassed the moment the real library is installed.
    """
    out = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        # bool is a subclass of int in Python; JSON says they are distinct.
        ok = any(
            (isinstance(doc, bool) if t == "boolean"
             else not isinstance(doc, bool) and isinstance(doc, _JSON_TYPES[t]))
            for t in allowed
        )
        if not ok:
            return [f"{where}: {doc!r} is not of type {allowed}"]
    if "enum" in schema and doc not in schema["enum"]:
        out.append(f"{where}: {doc!r} not in enum {schema['enum']}")
    if "const" in schema and doc != schema["const"]:
        out.append(f"{where}: {doc!r} != const {schema['const']!r}")
    if isinstance(doc, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in doc:
                out.append(f"{where}: missing required key {key!r}")
        for key, value in doc.items():
            if key in props:
                out.extend(_errors(value, props[key], f"{where}.{key}"))
            elif schema.get("additionalProperties") is False:
                out.append(f"{where}: additional key {key!r} is not allowed")
    return out


def _validate(doc) -> list[str]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    if _jsonschema is not None:
        validator = _jsonschema.Draft7Validator(schema)
        return [f"{'.'.join(str(p) for p in e.path)}: {e.message}"
                for e in validator.iter_errors(doc)]
    return _errors(doc, schema)


class SchemaSelfValidationTests(_TempRoot):
    """The defect T11 fixes: ``project.schema.json`` is
    ``additionalProperties: false`` and did not declare ``display_name`` or
    ``description``, which ``_upsert_metadata`` *always* writes — so every
    scaffolded project.json failed its own validator."""

    def assertValid(self, doc) -> None:
        errors = _validate(doc)
        self.assertEqual(errors, [], f"document fails project.schema.json: {errors}")

    def test_the_validator_actually_rejects(self) -> None:
        """Guard the guard: a passing validation must mean something."""
        self.assertTrue(_validate({"schema": 2}))                       # required
        self.assertTrue(_validate({"schema": 3, "pid": None, "name": None,
                                   "owner_user_id": None, "created_at": None}))
        self.assertTrue(_validate({"schema": 2, "pid": None, "name": None,
                                   "owner_user_id": None, "created_at": None,
                                   "nonsense": 1}))                     # additional

    def test_bundled_template_validates_and_is_schema_2(self) -> None:
        doc = json.loads(_TEMPLATE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], 2)
        self.assertValid(doc)

    def test_scaffolded_project_validates(self) -> None:
        project_layout.scaffold_project(
            "blackhole", display_name="Blackhole", description="Event horizon sim."
        )
        doc = self._read(project_layout.xo_dir("blackhole"))
        # The two keys that were being written but never declared.
        self.assertEqual(doc["display_name"], "Blackhole")
        self.assertEqual(doc["description"], "Event horizon sim.")
        self.assertValid(doc)

    def test_scaffolded_project_without_metadata_validates(self) -> None:
        """The defaults path: display_name falls back to the id, description
        to the empty string — both still written, both still declared."""
        project_layout.scaffold_project("bare")
        doc = self._read(project_layout.xo_dir("bare"))
        self.assertEqual(doc["display_name"], "bare")
        self.assertEqual(doc["description"], "")
        self.assertValid(doc)

    def test_scaffold_then_watcher_tick_validates(self) -> None:
        project_layout.scaffold_project("nova", display_name="Nova", description="d")
        xo = project_layout.xo_dir("nova")
        project_json.fill_identity(xo, "nova")
        doc = self._read(xo)
        self.assertTrue(doc["pid"])
        self.assertNotIn("_template", doc)
        self.assertValid(doc)

    def test_git_block_is_contractual(self) -> None:
        """T12/T13 populate ``git``; declaring it here is what makes writing
        it legal at all under ``additionalProperties: false``."""
        project_layout.scaffold_project("provenance")
        xo = project_layout.xo_dir("provenance")
        project_json.fill_identity(xo, "provenance")

        doc = self._read(xo)
        doc["git"] = {
            "remote_url": "https://github.com/owner/repo.git",
            "default_branch": "main",
        }
        self.assertValid(doc)

        # Not a repo, or no origin: both nulls, still valid.
        doc["git"] = {"remote_url": None, "default_branch": None}
        self.assertValid(doc)

        # And the block is closed, so a stray key is caught rather than
        # silently persisted into the durable record.
        doc["git"] = {"remote_url": None, "default_branch": None, "is_repo": True}
        self.assertTrue(_validate(doc))

    def test_version_1_documents_are_still_valid(self) -> None:
        """Nothing renumbers an existing document — the sink carries
        ``schema`` forward — so rejecting 1 would just move the "every file
        on disk fails its own validator" defect to another key."""
        xo = self._xo("legacy", {"schema": 1, "_template": True, "pid": None})
        project_json.fill_identity(xo, "legacy")
        doc = self._read(xo)
        self.assertEqual(doc["schema"], 1)
        self.assertValid(doc)

    def test_a_document_with_no_schema_key_is_minted_at_2(self) -> None:
        xo = self._xo("versionless", {"_template": True})
        project_json.fill_identity(xo, "versionless")
        self.assertEqual(self._read(xo)["schema"], 2)


class OwnershipTests(_TempRoot):
    """§5.1's ownership table, enforced rather than described: neither
    writer may touch the other's keys."""

    def test_sink_leaves_metadata_and_git_untouched(self) -> None:
        seeded = {
            "schema": 2,
            "_template": True,
            "pid": None,
            "name": None,
            "owner_user_id": None,
            "created_at": None,
            "display_name": "Blackhole",
            "description": "Event horizon sim.",
            "git": {"remote_url": "https://github.com/o/r.git",
                    "default_branch": "main"},
            "category": "research",
        }
        xo = self._xo("owned", seeded)

        self.assertTrue(project_json.fill_identity(xo, "owned"))

        after = self._read(xo)
        self.assertEqual(after["display_name"], "Blackhole")
        self.assertEqual(after["description"], "Event horizon sim.")
        self.assertEqual(after["git"], seeded["git"])
        self.assertEqual(after["category"], "research")

    def test_upsert_leaves_identity_and_git_untouched(self) -> None:
        """The other half of the table. ``_upsert_metadata`` runs on every
        scaffold call, including re-scaffolds of a live project."""
        project_layout.scaffold_project("live", display_name="Live")
        xo = project_layout.xo_dir("live")
        project_json.fill_identity(xo, "live")

        # Stand in for the T13 refresher and a hand-curated key.
        doc = self._read(xo)
        doc["git"] = {"remote_url": "https://github.com/o/r.git",
                      "default_branch": "main"}
        doc["category"] = "research"
        (xo / "project.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")

        project_layout.scaffold_project("live", description="Now described.")

        after = self._read(xo)
        self.assertEqual(after["pid"], doc["pid"])
        self.assertEqual(after["schema"], doc["schema"])
        self.assertEqual(after["owner_user_id"], doc["owner_user_id"])
        self.assertEqual(after["created_at"], doc["created_at"])
        self.assertEqual(after["git"], doc["git"])
        self.assertEqual(after["category"], "research")
        self.assertEqual(after["description"], "Now described.")
        self.assertEqual(after["display_name"], "Live")

    def test_upsert_never_deletes_a_key_it_was_not_given(self) -> None:
        """Under ``write_json_owned`` an owned key omitted from ``values``
        is a *deletion*, so a rename that supplies only ``description``
        must still re-declare ``display_name`` at its current value."""
        project_layout.scaffold_project("keeper", display_name="Keeper")

        meta = project_layout.scaffold_project("keeper", description="Kept.")

        self.assertEqual(meta["display_name"], "Keeper")
        self.assertEqual(
            self._read(project_layout.xo_dir("keeper"))["display_name"], "Keeper"
        )

    def test_upsert_writes_nothing_when_nothing_changed(self) -> None:
        project_layout.scaffold_project("quiet", display_name="Quiet", description="d")
        path = project_layout.xo_dir("quiet") / "project.json"
        before = path.read_text(encoding="utf-8")
        stamp = path.stat().st_mtime_ns

        project_layout.scaffold_project("quiet", display_name="Quiet", description="d")

        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_upsert_repairs_an_unparseable_document(self) -> None:
        """Unchanged behaviour, made explicit: there is no merge base to
        preserve, and unlike ``fill_identity`` this writer mints no ``pid``,
        so nothing recoverable is lost by rewriting."""
        pdir = self.root / "broken"
        (pdir / ".xo").mkdir(parents=True)
        (pdir / ".xo" / "project.json").write_text('{"pid": "x",', encoding="utf-8")

        meta = project_layout.scaffold_project("broken", display_name="Broken")

        self.assertEqual(meta["display_name"], "Broken")
        on_disk = self._read(pdir / ".xo")
        self.assertEqual(on_disk["name"], "broken")
        self.assertNotIn("pid", on_disk)


if __name__ == "__main__":
    unittest.main()
