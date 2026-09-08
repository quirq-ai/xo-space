"""``?types=`` is derived from the schema, so it cannot rot again.

`TIMELINE_TYPES` was a hardcoded frozenset of twelve, and by the time anyone
looked it had already fallen behind twice: `todo.status_changed` (widened by
T7) and all eight `workitem.*` types were being emitted, appended to
`timeline.jsonl` and served by an unfiltered `GET /timeline` — while naming any
of them in `?types=` returned `400 unknown timeline type(s)`. Nothing failed;
the filter just quietly could not see a third of the log.

That is defect R3a's shape exactly — an emitter widened and a declaration
lagged — and the fix is the one the route-parity gate already uses: assert the
invariant rather than pin the list.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from routers.cowork_agent.bff._visualizer_presenter import (
    TIMELINE_TYPES,
    parse_types_param,
)

_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "services" / "cowork_agent" / "visualizer" / "schema"
    / "timeline.schema.json"
)


def _schema_consts() -> set[str]:
    """The reachable branches, walked independently of the implementation."""
    doc = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    defs = doc.get("definitions") or {}
    out: set[str] = set()
    for branch in doc.get("oneOf") or []:
        ref = branch.get("$ref", "")
        node = defs.get(ref.rsplit("/", 1)[-1]) if ref else branch
        if isinstance(node, dict):
            const = ((node.get("properties") or {}).get("type") or {}).get("const")
            if isinstance(const, str) and const:
                out.add(const)
    return out


class DerivedFromSchemaTests(unittest.TestCase):
    def test_the_allowlist_equals_the_schema(self) -> None:
        """The invariant. Declared implies filterable, and the reverse."""
        self.assertEqual(set(TIMELINE_TYPES), _schema_consts())

    def test_it_is_not_empty(self) -> None:
        """An unreadable schema degrades to the empty set, which rejects
        everything. Green here means the file really was read."""
        self.assertGreater(len(TIMELINE_TYPES), 12)

    def test_the_types_that_had_silently_rotted_are_filterable(self) -> None:
        for name in (
            "todo.status_changed",
            "workitem.created", "workitem.adopted", "workitem.assigned",
            "workitem.claimed", "workitem.released", "workitem.closed",
            "workitem.reopened", "workitem.deleted",
        ):
            with self.subTest(name):
                self.assertIn(name, TIMELINE_TYPES)
                self.assertEqual(parse_types_param(name), frozenset({name}))

    def test_an_undeclared_type_is_still_rejected(self) -> None:
        """Deriving the set must not turn the allowlist into an any-list."""
        with self.assertRaises(Exception):
            parse_types_param("workitem.invented")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
