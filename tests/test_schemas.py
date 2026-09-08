"""The JSON Schemas, made load-bearing (docs/syncplan.md §8, T16).

Ten schemas sat under ``services/cowork_agent/visualizer/schema/`` as pure
documentation: nothing in the repo imported ``jsonschema``, and no code
read a ``schema`` field to branch or migrate. Documentation that nothing
checks drifts, and this set had — two records were violated by their own
writers:

* ``stats.schema.json`` — ``sinks/stats.py`` writes ``_session_totals``
  and ``_by_day_totals`` into a document declared
  ``additionalProperties: false``, so *every* ``stats.json`` on disk
  failed its own validator.
* ``project.schema.json`` — ``display_name`` / ``description`` were
  always written and never declared (fixed by T11).

And ``todos.json`` carried a ``$schema`` pointing at ``./schema/
todos.schema.json``, a ``.xo/schema/`` directory that has never existed.

So the suite below does three things, in ascending order of usefulness:

1. Every schema file is itself a valid Draft-07 schema and follows the
   ``$id`` convention the writers stamp into documents.
2. Every file the project template ships validates against its schema —
   the template is copied verbatim into every new project, so a template
   that fails is a violation replicated to every user.
3. Every *live writer's* output validates. This is the part that keeps
   working: a sink that starts writing an undeclared key fails here on
   the next run rather than years later when someone finally runs a
   validator.

Written as plain ``unittest.TestCase`` so ``unittest discover -s tests``
— the gate — needs no third-party runner. ``jsonschema`` itself comes
from ``requirements-dev.txt``; when it is absent the module skips rather
than erroring, so a contributor who has not installed the dev extras
still gets a green gate (and a visible skip).
"""

from __future__ import annotations

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

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer import todos_store
from services.cowork_agent.visualizer.ingest.events import (
    FileTouched,
    MessageObserved,
    SessionFirstSeen,
    ToolUseObserved,
    UsageObserved,
)
from services.cowork_agent.visualizer.sinks import activity as activity_sink
from services.cowork_agent.visualizer.sinks import project_json
from services.cowork_agent.visualizer.sinks import stats as stats_sink
from services.cowork_agent.visualizer.workspace import projects_json, space_json

_SKIP_REASON = (
    "jsonschema is not installed — "
    "uv pip install --python venv/bin/python -r requirements-dev.txt"
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "services" / "cowork_agent" / "visualizer" / "schema"
TEMPLATE_XO = ROOT / "services" / "cowork_agent" / "project_template" / ".xo"

#: Which schema governs which file the project template ships. Every JSON
#: file under the template's ``.xo/`` must appear here — see
#: :meth:`TemplateFileTests.test_no_template_file_is_unschema_d`, which is
#: what stops a new template file arriving with nothing checking it.
#:
#: ``stats.json``, ``timeline.jsonl``, ``sync.json`` and ``sessions/`` left
#: the template in syncplan T19: they are machine-local runtime state and now
#: live under ``~/.quirq/projects/<key>/``, so shipping a zeroed copy inside
#: every project would have put a second, permanently-stale file in the tree
#: that syncs. The schemas themselves are unchanged — the runtime writers
#: still validate against them (see :class:`StatsSinkTests`).
TEMPLATE_FILE_SCHEMAS: dict[str, str] = {
    "project.json": "project",
    "todos.json": "todos",
    "peers.json": "peers",
}

#: Files a scaffolded project's ``.xo/`` holds, mapped the same way.
SCAFFOLD_FILE_SCHEMAS = dict(TEMPLATE_FILE_SCHEMAS)

#: Files the template deliberately no longer ships, and which a scaffold must
#: therefore NOT create in the project tree. Pinned as the other half of the
#: move: ``project_layout.scaffold_project`` used to re-create
#: ``sessions/sessionslist.json`` after the template copy, which would have
#: silently undone the deletion at every scaffold.
RUNTIME_FILES_NOT_IN_THE_PROJECT_TREE = (
    "stats.json",
    "timeline.jsonl",
    "sync.json",
    "sessions",
    "sessions/sessionslist.json",
)


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def _schema_names() -> list[str]:
    return sorted(p.name[: -len(".schema.json")] for p in SCHEMA_DIR.glob("*.schema.json"))


@unittest.skipIf(Draft7Validator is None, _SKIP_REASON)
class _SchemaCase(unittest.TestCase):
    """Adds ``assertValid`` / ``assertInvalid`` to a plain TestCase."""

    def assertValid(self, document: object, schema_name: str, label: str) -> None:
        validator = Draft7Validator(_load_schema(schema_name))
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
        if errors:
            detail = "\n".join(
                f"  at /{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                for e in errors
            )
            self.fail(f"{label} fails {schema_name}.schema.json:\n{detail}")

    def assertInvalid(self, document: object, schema_name: str, label: str) -> None:
        validator = Draft7Validator(_load_schema(schema_name))
        if validator.is_valid(document):
            self.fail(f"{label} was accepted by {schema_name}.schema.json but must not be")


# ── 1. the schema files themselves ────────────────────────────────────────────


class SchemaFileTests(_SchemaCase):
    def test_every_schema_is_a_valid_draft7_schema(self) -> None:
        """A schema with a typo in it validates nothing and says nothing."""
        names = _schema_names()
        self.assertTrue(names, f"no schemas found under {SCHEMA_DIR}")
        for name in names:
            with self.subTest(schema=name):
                Draft7Validator.check_schema(_load_schema(name))

    def test_every_schema_declares_draft7(self) -> None:
        for name in _schema_names():
            with self.subTest(schema=name):
                self.assertEqual(
                    _load_schema(name).get("$schema"),
                    "http://json-schema.org/draft-07/schema#",
                )

    def test_every_schema_id_matches_its_filename(self) -> None:
        """``$id`` is the value writers stamp into a document's ``$schema``
        key (``workspace/projects_json.py``, ``workspace/space_json.py``,
        ``adapters/*/agents.py``, ``todos_store.py``). It is a logical id,
        never a path — a path pointed at a ``.xo/schema/`` directory that
        does not exist, which is the dangling pointer T16 removed.
        """
        for name in _schema_names():
            with self.subTest(schema=name):
                self.assertEqual(_load_schema(name).get("$id"), f"xo/{name}.schema.json")


class DanglingPointerTests(_SchemaCase):
    """The schemas live in exactly one place, and nothing may cite another."""

    #: The two spellings of the pointer T16 removed: a relative path into a
    #: ``.xo/schema/`` directory that has never been created by anything.
    BAD_POINTERS = ("project_template/.xo/schema", '"./schema/')

    def _python_sources(self) -> list[Path]:
        out: list[Path] = []
        for base in (ROOT / "services" / "cowork_agent", ROOT / "routers"):
            out.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
        return out

    def test_no_source_file_points_at_a_nonexistent_schema_directory(self) -> None:
        self.assertFalse(
            (TEMPLATE_XO / "schema").exists(),
            "a .xo/schema/ directory now exists — update this guard, or the "
            "pointer it protects against is no longer dangling",
        )
        offenders = [
            f"{p.relative_to(ROOT)}: {bad}"
            for p in self._python_sources()
            for bad in self.BAD_POINTERS
            if bad in p.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], f"dangling schema pointer reintroduced: {offenders}")

    def test_todos_documents_carry_the_schema_id(self) -> None:
        self.assertEqual(todos_store._SCHEMA_REF, _load_schema("todos")["$id"])


# ── 2. the project template ───────────────────────────────────────────────────


class TemplateFileTests(_SchemaCase):
    """The template is copied verbatim into every project (``project_layout.
    _copy_template``), so a template file that fails its schema is a
    violation replicated to every user of the product."""

    def test_no_template_file_is_unschema_d(self) -> None:
        shipped = sorted(
            str(p.relative_to(TEMPLATE_XO))
            for p in TEMPLATE_XO.rglob("*.json")
        )
        self.assertEqual(
            shipped,
            sorted(TEMPLATE_FILE_SCHEMAS),
            "a template .xo/ file was added or removed — map it to its schema "
            "in TEMPLATE_FILE_SCHEMAS so it is actually validated",
        )

    def test_every_template_file_validates(self) -> None:
        for rel, schema_name in sorted(TEMPLATE_FILE_SCHEMAS.items()):
            with self.subTest(template=rel):
                raw = (TEMPLATE_XO / rel).read_text(encoding="utf-8")
                document = json.loads(raw)  # an unparseable template is a failure too
                self.assertValid(document, schema_name, f"template .xo/{rel}")

    def test_the_template_ships_no_runtime_file(self) -> None:
        """The template is the synced tier only (syncplan T19)."""
        for rel in RUNTIME_FILES_NOT_IN_THE_PROJECT_TREE:
            with self.subTest(template=rel):
                self.assertFalse(
                    (TEMPLATE_XO / rel).exists(),
                    f"template .xo/{rel} is runtime state and must not ship "
                    "inside the project tree",
                )


# ── 3. what the live writers actually produce ─────────────────────────────────


class _TempProject(_SchemaCase):
    """Throwaway projects root and state root: nothing here may touch the
    developer's real ``~/xo-projects`` or ``~/.quirq``."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "projects"
        self.root.mkdir(parents=True)
        self.state = self.tmp / "state"
        self.state.mkdir(parents=True)
        env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.root),
                "QUIRQ_STATE_ROOT": str(self.state),
                # Force the bundled template even if the developer's shell
                # points XO_PROJECT_TEMPLATE elsewhere.
                "XO_PROJECT_TEMPLATE": "",
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    def _xo_dir(self, name: str = "demo") -> Path:
        xo = self.root / name / ".xo"
        xo.mkdir(parents=True, exist_ok=True)
        return xo


class ScaffoldedProjectTests(_TempProject):
    def test_a_freshly_scaffolded_project_validates(self) -> None:
        """Template + ``_upsert_metadata`` — the state a user's project is
        actually in the moment it is created."""
        project_layout.scaffold_project("Demo Project")
        xo = self.root / project_layout.resolve_project_dirname("Demo Project") / ".xo"
        for rel, schema_name in sorted(SCAFFOLD_FILE_SCHEMAS.items()):
            with self.subTest(scaffolded=rel):
                path = xo / rel
                self.assertTrue(path.is_file(), f"scaffold did not create .xo/{rel}")
                self.assertValid(
                    json.loads(path.read_text(encoding="utf-8")),
                    schema_name,
                    f"scaffolded .xo/{rel}",
                )

    def test_a_scaffold_creates_no_runtime_file_in_the_project_tree(self) -> None:
        """``scaffold_project`` used to re-seed ``sessions/sessionslist.json``
        after the template copy — the one line that would have undone T19's
        template deletion on every new project."""
        project_layout.scaffold_project("Demo Project")
        xo = self.root / project_layout.resolve_project_dirname("Demo Project") / ".xo"
        for rel in RUNTIME_FILES_NOT_IN_THE_PROJECT_TREE:
            with self.subTest(scaffolded=rel):
                self.assertFalse(
                    (xo / rel).exists(),
                    f"scaffold created .xo/{rel} in the synced tree",
                )


class StatsSinkTests(_TempProject):
    """``sinks/stats.py`` — the writer that violated its own schema."""

    TS = "2026-09-07T12:00:00Z"

    def _events(self) -> list:
        common = dict(native_session_id="nsid-1", runtime="demo_runtime", project_id="demo")
        return [
            SessionFirstSeen(ts=self.TS, cwd="/tmp/demo", **common),
            MessageObserved(ts=self.TS, role="user", **common),
            MessageObserved(ts=self.TS, role="assistant", model="m-1", **common),
            UsageObserved(
                ts=self.TS,
                input_tokens=120,
                output_tokens=45,
                cache_read_input_tokens=7,
                cache_creation_input_tokens=3,
                model="m-1",
                latency_ms=1234,
                **common,
            ),
            ToolUseObserved(ts=self.TS, tool="Bash", **common),
            FileTouched(ts=self.TS, relative_path="a.py", created=True, **common),
        ]

    def _write_stats(self) -> dict:
        xo = self._xo_dir()
        self.assertTrue(stats_sink.apply(xo, self._events()))
        return json.loads((xo / "stats.json").read_text(encoding="utf-8"))

    def test_sink_output_validates(self) -> None:
        self.assertValid(self._write_stats(), "stats", "stats.json as the sink writes it")

    def test_the_private_accumulators_are_declared(self) -> None:
        """The specific regression: the sink persists its working state
        in-band so a restart recovers it, into a document declared
        ``additionalProperties: false``. Undeclared, those two keys made
        every stats.json on disk invalid."""
        document = self._write_stats()
        self.assertIn("_session_totals", document)
        self.assertIn("_by_day_totals", document)
        declared = _load_schema("stats")["properties"]
        self.assertIn("_session_totals", declared)
        self.assertIn("_by_day_totals", declared)

    def test_a_genuinely_undeclared_key_is_still_rejected(self) -> None:
        """Declaring the two private keys must not have loosened the
        document — ``additionalProperties: false`` still has to bite."""
        document = self._write_stats()
        document["_totally_new_private_thing"] = {}
        self.assertInvalid(document, "stats", "stats.json with an undeclared key")

    def test_second_apply_still_validates(self) -> None:
        """The sink reads its own previous output back as the merge base,
        so the round trip is what has to validate, not just a first write."""
        xo = self._xo_dir()
        stats_sink.apply(xo, self._events())
        stats_sink.apply(xo, self._events())
        document = json.loads((xo / "stats.json").read_text(encoding="utf-8"))
        self.assertValid(document, "stats", "stats.json after a second tick")
        self.assertEqual(document["by_session"]["nsid-1"]["tokens"]["input"], 240)


class StatsCostTests(_SchemaCase):
    """syncplan §13 decision O6 — cost stays ``null``, never a confident
    ``0.0``. There is no pricing table in this repo, so a zero is a lie
    dressed as a measurement (the Sessions tab renders 16.5M real tokens
    as ``$0.00`` today). The schema now makes that lie structural."""

    def _stats_with(self, cost: object) -> dict:
        return {
            "schema": 2,
            "rolling": {
                "7d": {"tokens": {"input": 1, "output": 1}, "cost_usd": cost},
                "30d": {"tokens": {"input": 1, "output": 1}},
            },
        }

    def test_cost_is_representable_at_every_level(self) -> None:
        """Before T16 there was no cost property anywhere: cost was not
        zero, it was unrepresentable."""
        definitions = _load_schema("stats")["definitions"]
        for level in ("window", "session_stats", "session_totals_private", "day_bucket"):
            with self.subTest(level=level):
                self.assertIn("cost_usd", definitions[level]["properties"])

    def test_null_cost_is_valid(self) -> None:
        self.assertValid(self._stats_with(None), "stats", "cost_usd: null")

    def test_absent_cost_is_valid(self) -> None:
        document = self._stats_with(None)
        del document["rolling"]["7d"]["cost_usd"]
        self.assertValid(document, "stats", "cost_usd absent")

    def test_a_real_cost_is_valid(self) -> None:
        self.assertValid(self._stats_with(1.75), "stats", "cost_usd: 1.75")

    def test_zero_cost_is_rejected(self) -> None:
        self.assertInvalid(self._stats_with(0.0), "stats", "cost_usd: 0.0")

    def test_negative_cost_is_rejected(self) -> None:
        self.assertInvalid(self._stats_with(-1.0), "stats", "cost_usd: -1.0")


class StatsPhantomCounterTests(_SchemaCase):
    """``day_bucket.messages.toolResults`` / ``errors`` were ``required``
    and have no producer — nothing in ``sinks/stats.py`` increments
    either, so they are permanently zero. Required implies meaning; they
    are optional now so they stay droppable."""

    def test_phantom_counters_are_not_required(self) -> None:
        required = _load_schema("stats")["definitions"]["day_bucket"]["properties"]["messages"]["required"]
        self.assertNotIn("toolResults", required)
        self.assertNotIn("errors", required)

    def test_the_counters_that_do_have_producers_stay_required(self) -> None:
        required = _load_schema("stats")["definitions"]["day_bucket"]["properties"]["messages"]["required"]
        self.assertEqual(sorted(required), ["assistant", "toolCalls", "total", "user"])


class TodosStoreTests(_TempProject):
    """``todos_store`` is the single writer of ``todos.json`` (T7/T8)."""

    def _todos_path(self) -> Path:
        return self._xo_dir() / "todos.json"

    def test_document_validates_through_the_whole_lifecycle(self) -> None:
        path = self._todos_path()
        created = todos_store.create_todo(
            path, runtime="demo_runtime", content="write the validator"
        )
        self.assertValid(
            json.loads(path.read_text(encoding="utf-8")), "todos", "todos.json after create"
        )

        todos_store.update_todo(path, created["id"], status="in_progress")
        self.assertValid(
            json.loads(path.read_text(encoding="utf-8")), "todos", "todos.json after update"
        )

        todos_store.delete_todo(path, created["id"])
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertValid(document, "todos", "todos.json after soft delete")
        self.assertEqual(document["$schema"], "xo/todos.schema.json")
        self.assertEqual(document["schema"], todos_store.TODOS_SCHEMA)

    def test_the_template_document_is_the_same_revision_the_store_writes(self) -> None:
        """A scaffolded project must not claim schema 1 while the only
        writer of the file stamps schema 2 — a reader that branches on the
        version would take the wrong branch on a brand-new project."""
        template = json.loads((TEMPLATE_XO / "todos.json").read_text(encoding="utf-8"))
        self.assertEqual(template["schema"], todos_store.TODOS_SCHEMA)


class ProjectJsonSinkTests(_TempProject):
    """``project.schema.json`` was the other writer-violates-own-schema
    record (T11). Identity fill is the writer."""

    def test_identity_fill_output_validates(self) -> None:
        xo = self._xo_dir("research-notes")
        (xo / "project.json").write_text(
            json.dumps({"schema": 2, "_template": True}), encoding="utf-8"
        )
        self.assertTrue(project_json.fill_identity(xo, "research-notes"))
        self.assertValid(
            json.loads((xo / "project.json").read_text(encoding="utf-8")),
            "project",
            "project.json after identity fill",
        )

    def test_scaffold_then_fill_validates(self) -> None:
        """The realistic order: ``_upsert_metadata`` writes display_name /
        description, then the watcher tick fills identity."""
        project_layout.scaffold_project("Research Notes", description="a description")
        pid = project_layout.resolve_project_dirname("Research Notes")
        xo = self.root / pid / ".xo"
        project_json.fill_identity(xo, pid)
        self.assertValid(
            json.loads((xo / "project.json").read_text(encoding="utf-8")),
            "project",
            "project.json after scaffold + identity fill",
        )


class ActivitySinkTests(_TempProject):
    """``activity.json`` has no template file — the sink is its only
    producer, so the sink's output is the only fixture there is."""

    def test_sink_output_validates(self) -> None:
        path = self.state / "activity" / "projects" / "demo.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.assertTrue(
            activity_sink.apply(
                path,
                [
                    {
                        "session_id": "nsid-1",
                        "runtime": "demo_runtime",
                        "started_at_ms": 1_757_000_000_000,
                        "updated_at_ms": 1_757_000_060_000,
                    }
                ],
                model_by_session={"nsid-1": "model-1"},
                host="test-host",
            )
        )
        self.assertValid(
            json.loads(path.read_text(encoding="utf-8")), "activity", "activity.json"
        )

    def test_empty_snapshot_validates(self) -> None:
        path = self.state / "activity" / "projects" / "empty.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        activity_sink.apply(path, [], model_by_session={})
        self.assertValid(
            json.loads(path.read_text(encoding="utf-8")), "activity", "empty activity.json"
        )


class WorkspaceRecordTests(_TempProject):
    """The two workspace-tier records (syncplan §5.2, §5.3). Neither has a
    template file — the writer is the only fixture there is."""

    def setUp(self) -> None:
        super().setUp()
        # Both writers memoize; the other suites in this process leave those
        # caches warm, and a warm cache turns apply() into a no-op that would
        # validate someone else's document (or none at all).
        projects_json.reset_caches()
        self.addCleanup(projects_json.reset_caches)
        space_json._last_build = 0.0
        self.addCleanup(setattr, space_json, "_last_build", 0.0)
        # These two resolve the XO root on every call, so the read-back has to
        # happen inside the patch too. HOME is redirected as well: nothing
        # here may reach the developer's real home.
        home = patch.dict(os.environ, {"HOME": str(self.tmp)}, clear=False)
        home.start()
        self.addCleanup(home.stop)

    def _seed(self, *names: str) -> None:
        for name in names:
            (self.root / name / ".xo").mkdir(parents=True, exist_ok=True)
            (self.root / name / ".xo" / "project.json").write_text(
                json.dumps({"schema": 2, "name": name, "pid": f"pid-{name}"}),
                encoding="utf-8",
            )

    def test_projects_registry_validates(self) -> None:
        self._seed("alpha", "beta")
        projects_json.apply()
        self.assertValid(
            json.loads(projects_json.path().read_text(encoding="utf-8")),
            "projects",
            "projects.json as the workspace writer writes it",
        )

    def test_an_empty_workspace_still_validates(self) -> None:
        projects_json.apply()
        self.assertValid(
            json.loads(projects_json.path().read_text(encoding="utf-8")),
            "projects",
            "projects.json for an empty workspace",
        )

    def test_space_record_validates(self) -> None:
        self._seed("alpha")
        with patch.dict(os.environ, {"CODER_WORKSPACE_ID": "ws-42"}, clear=False):
            space_json.apply()
            document = json.loads(space_json.path().read_text(encoding="utf-8"))
        self.assertValid(document, "space", "space.json as the record writer writes it")

    def test_space_record_validates_off_coder(self) -> None:
        """syncplan O1: no CODER_WORKSPACE_ID means ``space_id: null``, which
        the schema has to accept — a null there is the documented state, not
        a degraded one."""
        self._seed("alpha")
        env = {k: v for k, v in os.environ.items() if k != "CODER_WORKSPACE_ID"}
        with patch.dict(os.environ, env, clear=True):
            space_json.apply()
            document = json.loads(space_json.path().read_text(encoding="utf-8"))
        self.assertIsNone(document["space_id"])
        self.assertValid(document, "space", "space.json off Coder")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
