"""The todo status vocabulary has exactly one definition.

``services/cowork_agent/visualizer/todo_status.py`` owns the set. Python
callers import it, so they cannot drift. The copies that *cannot* import
it — two JSON Schemas, three ``space_ui`` files and two docs — are held
to it here instead: every assertion below derives its expectation from
:data:`TODO_STATUSES`, so adding or renaming a status makes this module
name each file that still disagrees.

Also the first coverage of todo write behaviour (the plan's §14 notes
there was none): the store's status gate is exercised end to end against
a temporary file, never the real ``~/xo-projects`` or ``~/.quirq``.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import get_args
from unittest.mock import patch

from pydantic import ValidationError

from routers.cowork_agent.bff._visualizer_models import (
    CreateTodoRequest,
    Todo,
    UpdateTodoRequest,
)
from services.cowork_agent.visualizer import todos_store
from services.cowork_agent.visualizer.sinks import sessions_augment
from services.cowork_agent.visualizer.todo_status import (
    TODO_STATUSES,
    VALID_TODO_STATUSES,
    TodoStatus,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "services" / "cowork_agent" / "visualizer" / "schema"

BOGUS = "in-progress"   # a plausible typo: hyphen instead of underscore


def read(*parts: str) -> str:
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def js_object_keys(source: str, name: str) -> set[str]:
    """Keys of a flat ``const NAME={a:1,b:2}`` literal (may wrap lines)."""
    m = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*\{(.*?)\}", source, re.S)
    assert m, f"{name} not found — did the identifier get renamed?"
    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", m.group(1)))


def js_set_members(source: str, name: str) -> set[str]:
    """Members of a ``const NAME=new Set(['a','b'])`` literal."""
    m = re.search(
        r"const\s+" + re.escape(name) + r"\s*=\s*new\s+Set\(\[(.*?)\]\)", source, re.S
    )
    assert m, f"{name} not found — did the identifier get renamed?"
    return set(re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)))


class TodoStatusConstantTests(unittest.TestCase):
    """The constant itself, and the two Python modules that import it."""

    def test_vocabulary_is_the_five_lifecycle_values(self) -> None:
        self.assertEqual(
            TODO_STATUSES,
            ("pending", "in_progress", "completed", "cancelled", "blocked"),
        )
        self.assertEqual(len(set(TODO_STATUSES)), len(TODO_STATUSES))
        self.assertEqual(VALID_TODO_STATUSES, frozenset(TODO_STATUSES))
        # The typed form is derived from the tuple, so it cannot drift.
        self.assertEqual(get_args(TodoStatus), TODO_STATUSES)

    def test_store_and_sink_share_the_object_rather_than_a_copy(self) -> None:
        """Identity, not equality: an equal-but-separate frozenset would be
        a re-listing that silently survives the next status change."""
        self.assertIs(todos_store.VALID_STATUSES, VALID_TODO_STATUSES)
        self.assertIs(sessions_augment._VALID_STATUSES, VALID_TODO_STATUSES)

    def test_no_python_site_re_lists_the_set(self) -> None:
        """Guards the acceptance criterion directly: a source file that
        still spells the five values out is a second source of truth."""
        owner = "services/cowork_agent/visualizer/todo_status.py"
        for rel in (
            owner,
            "services/cowork_agent/visualizer/todos_store.py",
            "services/cowork_agent/visualizer/sinks/sessions_augment.py",
            "services/cowork_agent/visualizer/ingest/events.py",
            "routers/cowork_agent/bff/_visualizer_models.py",
        ):
            source = read(*rel.split("/"))
            # "blocked" is the tell: it appears in every enumeration of the
            # set and nowhere else in these modules.
            occurrences = source.count("blocked")
            if rel == owner:
                self.assertGreater(occurrences, 0, "the owner lost the value")
            else:
                self.assertEqual(
                    occurrences, 0,
                    f"{rel} enumerates the status set instead of importing "
                    f"it ({occurrences} occurrences of 'blocked')",
                )

    def test_task_counters_have_one_bucket_per_status(self) -> None:
        counters = sessions_augment._empty_row()["taskCount"]
        self.assertEqual(set(counters), {"total"} | set(TODO_STATUSES))
        self.assertEqual(set(counters.values()), {0})


class JsonSchemaAgreementTests(unittest.TestCase):
    """The on-disk contracts declare the same set."""

    def test_todos_schema_enum(self) -> None:
        schema = json.loads((SCHEMA_DIR / "todos.schema.json").read_text())
        enum = schema["definitions"]["todo"]["properties"]["status"]["enum"]
        self.assertEqual(enum, list(TODO_STATUSES))

    def test_sessions_augment_task_count_properties(self) -> None:
        schema = json.loads((SCHEMA_DIR / "sessions-augment.schema.json").read_text())
        props = schema["definitions"]["task_count"]["properties"]
        self.assertEqual(set(props), {"total"} | set(TODO_STATUSES))
        # additionalProperties:false means a status without a property here
        # makes a legal augment file fail validation once T16 lands.
        self.assertIs(schema["definitions"]["task_count"]["additionalProperties"], False)


class WireModelTests(unittest.TestCase):
    """``_visualizer_models.py`` declares the enum it used to omit."""

    def test_todo_status_is_declared_in_the_openapi_schema(self) -> None:
        prop = Todo.model_json_schema()["properties"]["status"]
        # Pydantic renders a Literal as an ``enum`` (or ``const`` for one
        # value); either way the declared values must be the vocabulary.
        declared = prop.get("enum") or [prop.get("const")]
        self.assertEqual(list(declared), list(TODO_STATUSES))

    def test_every_status_round_trips_and_a_typo_is_rejected(self) -> None:
        for status in TODO_STATUSES:
            self.assertEqual(
                Todo(id="a1b2c3d4", content="x", status=status).status, status
            )
        with self.assertRaises(ValidationError):
            Todo(id="a1b2c3d4", content="x", status=BOGUS)

    def test_request_bodies_stay_permissive_so_the_400_contract_holds(self) -> None:
        """Enforcement stays in the store, which raises ``invalid_status``
        and is mapped to ``400`` (todos-http-api.md:44). Typing the request
        bodies as the Literal would silently turn that into a 422."""
        self.assertEqual(
            CreateTodoRequest(runtime="codex", content="x", status=BOGUS).status, BOGUS
        )
        self.assertEqual(UpdateTodoRequest(status=BOGUS).status, BOGUS)


class FrontendAndDocAgreementTests(unittest.TestCase):
    """The copies that cannot import the constant."""

    def test_projects_view_sort_order(self) -> None:
        source = read("space_ui", "js", "views", "projects.js")
        self.assertEqual(js_object_keys(source, "ST_ORDER"), set(TODO_STATUSES))

    def test_atlas_view_order_done_set_and_colours(self) -> None:
        """The Atlas copy is a deliberate duplicate of the Projects one
        (views never import each other); duplicated does not mean free to
        drift."""
        source = read("space_ui", "js", "views", "atlas.js")
        self.assertEqual(js_object_keys(source, "ST_ORDER"), set(TODO_STATUSES))
        self.assertEqual(js_object_keys(source, "ST_COLOR"), set(TODO_STATUSES))
        done = js_set_members(source, "ST_DONE")
        self.assertTrue(
            done < set(TODO_STATUSES),
            f"ST_DONE {sorted(done)} is not a proper subset of the vocabulary",
        )

    def test_the_two_views_agree_with_each_other(self) -> None:
        atlas = read("space_ui", "js", "views", "atlas.js")
        projects = read("space_ui", "js", "views", "projects.js")
        self.assertEqual(
            js_object_keys(atlas, "ST_ORDER"), js_object_keys(projects, "ST_ORDER")
        )

    def test_one_chip_class_per_status(self) -> None:
        css = read("space_ui", "css", "projects.css")
        chips = set(re.findall(r"\.tchip\.st-([A-Za-z0-9_]+)", css))
        self.assertEqual(chips, set(TODO_STATUSES))

    def test_wiki_prose_lists_the_vocabulary(self) -> None:
        wiki = read("space_ui", "js", "views", "wiki.js")
        m = re.search(r"<dt>Status values</dt><dd>([^<]+)</dd>", wiki)
        self.assertIsNotNone(m, "the wiki's todos.json card lost its status row")
        listed = [v.strip() for v in m.group(1).split(",")]
        self.assertEqual(listed, list(TODO_STATUSES))

    def test_agent_facing_http_doc_lists_the_vocabulary(self) -> None:
        doc = read(
            ".agents", "skills", "xo-projects", "references", "todos-http-api.md"
        )
        m = re.search(r"Valid statuses:\s*`([^`]+)`", doc)
        self.assertIsNotNone(m, "todos-http-api.md lost its status enumeration")
        listed = [v.strip() for v in m.group(1).split("|")]
        self.assertEqual(listed, list(TODO_STATUSES))


class TodosStoreStatusGateTests(unittest.TestCase):
    """The one enforcement point, exercised end to end on a temp file.

    Never touches the real project or state roots — both are redirected
    into the temp dir and both helpers re-read the env on every call.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.todos_path = tmp / "project" / ".xo" / "todos.json"
        self.todos_path.parent.mkdir(parents=True)
        self._env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(tmp / "xo-projects"),
            "QUIRQ_STATE_ROOT": str(tmp / "quirq"),
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_create_accepts_every_status_in_the_vocabulary(self) -> None:
        for status in TODO_STATUSES:
            todo = todos_store.create_todo(
                self.todos_path, runtime="codex", content=f"c-{status}", status=status
            )
            self.assertEqual(todo["status"], status)
        on_disk = json.loads(self.todos_path.read_text())
        written = {t["status"] for t in on_disk["sessions"]["_project"]["todos"]}
        self.assertEqual(written, set(TODO_STATUSES))

    def test_update_accepts_every_status_in_the_vocabulary(self) -> None:
        todo = todos_store.create_todo(
            self.todos_path, runtime="codex", content="c"
        )
        self.assertEqual(todo["status"], "pending")
        for status in TODO_STATUSES:
            updated = todos_store.update_todo(
                self.todos_path, todo["id"], status=status
            )
            self.assertEqual(updated["status"], status)

    def test_a_status_outside_the_vocabulary_is_rejected_on_both_paths(self) -> None:
        with self.assertRaises(todos_store.TodosStoreError) as created:
            todos_store.create_todo(
                self.todos_path, runtime="codex", content="c", status=BOGUS
            )
        self.assertEqual(created.exception.code, "invalid_status")
        # The message quotes the vocabulary, so it stays accurate for free.
        for status in TODO_STATUSES:
            self.assertIn(status, created.exception.message)
        self.assertFalse(self.todos_path.exists(), "a rejected create still wrote")

        todo = todos_store.create_todo(self.todos_path, runtime="codex", content="c")
        with self.assertRaises(todos_store.TodosStoreError) as updated:
            todos_store.update_todo(self.todos_path, todo["id"], status=BOGUS)
        self.assertEqual(updated.exception.code, "invalid_status")
        self.assertEqual(
            json.loads(self.todos_path.read_text())
            ["sessions"]["_project"]["todos"][0]["status"],
            "pending",
            "a rejected update mutated the stored todo",
        )


if __name__ == "__main__":
    unittest.main()
