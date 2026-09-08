"""``<project>/.xo/agent.json`` has a schema, and its three writers are atomic.

docs/syncplan.md §5.4 · §8 T15.

Two defects, one file:

* **No schema.** ``agent.json`` was the only synced-tier record with no entry
  under ``visualizer/schema/`` — an undocumented contract three adapters write
  and three adapters read.
* **Non-atomic writes.** All three ``_write`` helpers did a bare
  ``path.write_text(json.dumps(...))``. A reader that catches the truncated
  document mid-write does not see a *warning*: ``_load`` swallows the
  ``JSONDecodeError`` and returns ``None``, which every caller reads as "no
  such agent" — the record silently disappears from the sidebar and from
  ``get_detail``.

Two constraints the schema has to respect, and both are tested here rather
than trusted:

* ``backend`` is an **open string**. An enumeration would name backends in a
  core file (forbidden outside ``adapters/<name>/``, ``config/agents/<name>/``
  and ``config/models/<name>/``) and would mean a new backend could not
  represent its own record without a core edit.
* **Every record already on disk still validates**, including the untagged
  ones the T5 ownership carve-out exists for. Nothing is required, because no
  reader keys on a field of this document — they key on the directory name.

The validator is ``jsonschema`` when it is installed (T16 adds it to
``requirements-dev.txt``) and a small draft-07 subset otherwise, so this file
is meaningful today and gets stricter for free when T16 lands.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.responses import JSONResponse

from services.cowork_agent.adapters.antigravity import agents as ag_agents
from services.cowork_agent.adapters.claude_code import agents as cc_agents
from services.cowork_agent.adapters.codex import agents as cx_agents
from services.cowork_agent.registry.adapter_registry import list_adapters

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "cowork_agent"
    / "visualizer"
    / "schema"
    / "agent.schema.json"
)

# (module, backend tag) for the three adapters that share this record.
AGENTS_CAPS = [
    (ag_agents, "antigravity"),
    (cc_agents, "claude_code"),
    (cx_agents, "codex"),
]

try:  # pragma: no cover - depends on whether T16's dev deps are installed
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None


# ── A draft-07 subset validator, used only when ``jsonschema`` is absent ──────
#
# It covers exactly what agent.schema.json uses: ``type`` (single or list),
# ``const``, ``enum``, ``properties``, ``required`` and ``additionalProperties``.
# ``format`` is annotation-only in draft-07 and is not asserted by ``jsonschema``
# either without a format checker, so it is skipped here too — the two
# validators agree on every case in this file.

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _subset_errors(instance, schema, where: str = "<root>") -> list[str]:
    errors: list[str] = []
    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else list(declared)
        if not any(_TYPES[name](instance) for name in names):
            # Nothing below can be checked meaningfully once the type is wrong.
            return [f"{where}: {instance!r} is not of type {names}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{where}: {instance!r} is not the constant {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{where}: {instance!r} is not one of {schema['enum']!r}")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{where}: {key!r} is a required property")
        for key, value in instance.items():
            if key in properties:
                errors += _subset_errors(value, properties[key], f"{where}.{key}")
            elif extra is False:
                errors.append(f"{where}: additional property {key!r} is not allowed")
            elif isinstance(extra, dict):
                errors += _subset_errors(value, extra, f"{where}.{key}")
    return errors


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


class _SchemaAssertions(unittest.TestCase):
    """Mixin: ``assertValid`` / ``assertInvalid`` against agent.schema.json."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_schema()

    def _errors(self, instance) -> list[str]:
        if jsonschema is not None:  # pragma: no cover - branch depends on deps
            return [e.message for e in jsonschema.Draft7Validator(self.schema).iter_errors(instance)]
        return _subset_errors(instance, self.schema)

    def assertValid(self, instance, msg: str = "") -> None:
        errors = self._errors(instance)
        self.assertEqual(errors, [], f"{msg}\ninstance: {instance!r}")

    def assertInvalid(self, instance, msg: str = "") -> None:
        self.assertNotEqual(self._errors(instance), [], f"{msg}\ninstance: {instance!r}")


class SchemaShapeTests(_SchemaAssertions):
    """The schema exists and says what §5.4 says it says."""

    def test_schema_file_exists_next_to_the_other_record_schemas(self) -> None:
        self.assertTrue(SCHEMA_PATH.is_file(), f"{SCHEMA_PATH} is missing")
        siblings = {p.name for p in SCHEMA_PATH.parent.glob("*.schema.json")}
        self.assertIn("agent.schema.json", siblings)

    def test_schema_declares_draft7_and_its_own_id(self) -> None:
        self.assertEqual(self.schema["$schema"], "http://json-schema.org/draft-07/schema#")
        self.assertEqual(self.schema["$id"], "xo/agent.schema.json")
        self.assertEqual(self.schema["$id"], f"xo/{SCHEMA_PATH.name}")
        self.assertEqual(self.schema["type"], "object")

    @unittest.skipIf(jsonschema is None, "jsonschema not installed (T16 adds it)")
    def test_schema_is_a_valid_draft7_schema(self) -> None:  # pragma: no cover
        jsonschema.Draft7Validator.check_schema(self.schema)

    def test_backend_is_an_open_string_with_no_enumeration(self) -> None:
        """A new backend must be representable without a core schema edit."""
        backend = self.schema["properties"]["backend"]
        self.assertIn("string", backend["type"])
        self.assertNotIn("enum", backend)
        self.assertNotIn("const", backend)
        self.assertNotIn("pattern", backend)

    def test_the_schema_names_no_backend(self) -> None:
        """Modularity invariant: ``visualizer/schema/`` is core code, so no
        adapter name may appear anywhere in the file — not in an enum, not in
        an example, not in prose."""
        text = SCHEMA_PATH.read_text(encoding="utf-8")
        for name in list_adapters():
            with self.subTest(backend=name):
                self.assertNotIn(name, text)

    def test_the_document_is_open(self) -> None:
        """``agent.json`` is adapter-owned: an adapter must be able to carry
        its own fields without editing this core schema."""
        self.assertIs(self.schema.get("additionalProperties"), True)
        self.assertValid(
            {"backend": "some-future-backend", "some_adapter_field": {"nested": 1}},
            "an adapter's own field must not need a schema edit",
        )

    def test_nothing_is_required(self) -> None:
        """No reader keys on a field of this record — they key on the project
        directory name — and untagged records predate every field but ``id``.
        A required key would strand data instead of describing it."""
        self.assertEqual(self.schema.get("required", []), [])
        self.assertValid({}, "an empty record must still validate")


class ExistingRecordsValidateTests(_SchemaAssertions):
    """Every shape any writer has ever produced, and the hand-written ones."""

    def test_the_shape_written_before_this_schema_existed(self) -> None:
        self.assertValid(
            {
                "id": "blackhole",
                "name": "Blackhole",
                "description": "",
                "backend": "some-backend",
                "created_at": "2026-09-01T09:00:00Z",
            },
            "the pre-T15 record shape must not be invalidated",
        )

    def test_the_untagged_legacy_record(self) -> None:
        """THE T5 CARVE-OUT, on the schema side: a record with no ``backend``
        predates the tag and is still on disk. Making ``backend`` required
        would declare every one of them invalid."""
        self.assertValid(
            {"id": "legacyproj", "name": "Legacy", "created_at": "2025-01-01T00:00:00Z"},
            "an untagged record must validate",
        )

    def test_an_empty_or_null_backend_validates(self) -> None:
        """``_load_owned`` treats empty and null the same as absent, so the
        schema must accept what the readers accept."""
        for value in ("", None):
            with self.subTest(backend=value):
                self.assertValid({"id": "blankproj", "backend": value})

    def test_the_shape_written_now(self) -> None:
        self.assertValid(
            {
                "$schema": "xo/agent.schema.json",
                "schema": 1,
                "id": "blackhole",
                "name": "Blackhole",
                "description": "",
                "backend": "some-backend",
                "created_at": "2026-09-01T09:00:00Z",
            }
        )

    def test_a_wrongly_typed_record_is_rejected(self) -> None:
        """The schema is not vacuous just because nothing is required."""
        self.assertInvalid({"backend": 42}, "backend must be a string")
        self.assertInvalid({"name": ["Blackhole"]}, "name must be a string")
        self.assertInvalid({"schema": "1"}, "schema must be an integer")
        self.assertInvalid({"schema": 2}, "there is no version 2 yet")
        self.assertInvalid([], "the document is an object")


class _TempRoot(unittest.TestCase):
    """Throwaway projects root, state root and project template — never the
    developer's real ``~/xo-projects``, ``~/.quirq`` or template."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir(parents=True)
        state = Path(self._tmp.name) / "state"
        state.mkdir(parents=True)
        # An empty template: scaffolding's file set is not what this file is
        # about, and copying the real one would couple these tests to it.
        template = Path(self._tmp.name) / "template"
        template.mkdir(parents=True)
        legacy = Path(self._tmp.name) / "claude-cowork"
        legacy.mkdir(parents=True)
        env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.root),
                "QUIRQ_STATE_ROOT": str(state),
                "XO_PROJECT_TEMPLATE": str(template),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        legacy_patch = patch.object(cc_agents, "CLAUDE_COWORK_DIR", legacy)
        legacy_patch.start()
        self.addCleanup(legacy_patch.stop)

    def record_path(self, agent_id: str) -> Path:
        return self.root / agent_id / ".xo" / "agent.json"

    def write_record(self, agent_id: str, record: dict) -> Path:
        path = self.record_path(agent_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return path

    def create(self, module, agent_id: str, *, name: str, description: str = ""):
        class _Body:
            pass

        body = _Body()
        body.id = agent_id
        body.name = name
        body.description = description
        result = module.create_agent(body)
        self.assertNotIsInstance(
            result,
            JSONResponse,
            f"create_agent failed: {getattr(result, 'body', result)!r}",
        )
        return result


class WriterOutputValidatesTests(_TempRoot, _SchemaAssertions):
    """What the three writers actually put on disk, validated."""

    def test_every_adapter_writes_a_valid_record(self) -> None:
        for module, tag in AGENTS_CAPS:
            with self.subTest(backend=tag):
                agent_id = f"created-{tag.replace('_', '-')}"
                self.create(module, agent_id, name=f"Created {tag}", description="x")
                on_disk = json.loads(self.record_path(agent_id).read_text(encoding="utf-8"))
                self.assertValid(on_disk, f"{tag} wrote a record its own schema rejects")
                self.assertEqual(on_disk["backend"], tag)
                self.assertEqual(on_disk["schema"], 1)
                self.assertEqual(on_disk["$schema"], "xo/agent.schema.json")

    def test_a_patched_record_still_validates_and_keeps_its_tag(self) -> None:
        for module, tag in AGENTS_CAPS:
            with self.subTest(backend=tag):
                agent_id = f"patched-{tag.replace('_', '-')}"
                self.create(module, agent_id, name="Before")

                class _Body:
                    model_fields_set = {"name", "description"}
                    name = "After"
                    description = "patched"

                self.assertIsNotNone(module.patch(agent_id, _Body()))
                on_disk = json.loads(self.record_path(agent_id).read_text(encoding="utf-8"))
                self.assertValid(on_disk, f"{tag} patched a record into an invalid shape")
                self.assertEqual(on_disk["name"], "After")
                self.assertEqual(on_disk["backend"], tag)
                # The version stamp survives a patch — patch rewrites the whole
                # document it read, so dropping it here would be silent.
                self.assertEqual(on_disk["schema"], 1)

    def test_the_t5_carve_out_is_untouched(self) -> None:
        """T15 must not narrow ownership: a record this writer tags is claimed
        by exactly one adapter, and an untagged one by all three."""
        self.create(cx_agents, "ownedproj", name="Owned")
        claimants = [tag for mod, tag in AGENTS_CAPS if mod.get_detail("ownedproj") is not None]
        self.assertEqual(claimants, ["codex"])

        self.write_record("legacyproj", {"id": "legacyproj", "name": "Legacy"})
        claimants = [tag for mod, tag in AGENTS_CAPS if mod.get_detail("legacyproj") is not None]
        self.assertEqual(sorted(claimants), ["antigravity", "claude_code", "codex"])


class AtomicWriteTests(_TempRoot):
    """The write is a temp file plus ``os.replace``, not a bare ``write_text``."""

    def test_write_goes_through_the_shared_atomic_primitive(self) -> None:
        for module, tag in AGENTS_CAPS:
            with self.subTest(backend=tag):
                with patch.object(module, "write_json_atomic") as spy:
                    module._write("someproj", {"id": "someproj", "backend": tag})
                spy.assert_called_once()
                path, data = spy.call_args.args
                self.assertEqual(path, self.root / "someproj" / ".xo" / "agent.json")
                self.assertEqual(data["backend"], tag)

    def test_no_bare_write_text_remains_in_the_record_writer(self) -> None:
        """Regression guard: the non-atomic write must not come back."""
        for module, tag in AGENTS_CAPS:
            with self.subTest(backend=tag):
                source = Path(module.__file__).read_text(encoding="utf-8")
                self.assertNotIn("path.write_text(json.dumps(data", source)

    def test_the_write_creates_its_directory_and_leaves_no_temp_file(self) -> None:
        """``write_json_atomic`` does the ``mkdir`` the old helper did by hand,
        and its sibling temp file must not survive the write."""
        for module, tag in AGENTS_CAPS:
            with self.subTest(backend=tag):
                agent_id = f"fresh-{tag.replace('_', '-')}"
                module._write(agent_id, {"id": agent_id, "backend": tag})
                xo = self.root / agent_id / ".xo"
                self.assertEqual(
                    sorted(p.name for p in xo.iterdir()),
                    ["agent.json"],
                    "a leftover temp file is a half-written record another "
                    "adapter can pick up",
                )
                text = (xo / "agent.json").read_text(encoding="utf-8")
                self.assertEqual(json.loads(text)["backend"], tag)
                self.assertTrue(text.endswith("\n"))

    def test_a_reader_never_sees_a_truncated_record(self) -> None:
        """The point of atomicity, from the reader's side: while the new
        document is being written the old one is still whole and parseable,
        because the bytes land in a sibling file first."""
        agent_id = "torn"
        cx_agents._write(agent_id, {"id": agent_id, "backend": "codex", "name": "before"})
        path = self.record_path(agent_id)

        seen: list = []
        real_replace = os.replace

        def _peek(src, dst):
            # Mid-write: everything already written went to the temp file.
            seen.append(json.loads(Path(dst).read_text(encoding="utf-8")))
            return real_replace(src, dst)

        with patch("services.cowork_agent.visualizer.atomic_write.os.replace", _peek):
            cx_agents._write(agent_id, {"id": agent_id, "backend": "codex", "name": "after"})

        self.assertEqual(seen, [{"id": agent_id, "backend": "codex", "name": "before"}])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["name"], "after")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
