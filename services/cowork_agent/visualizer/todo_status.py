"""The todo lifecycle status vocabulary — one definition, one place.

Before this module the set ``{pending, in_progress, completed,
cancelled, blocked}`` was spelled out in fourteen places (Python,
two JSON Schemas, the wire model, three frontend files and two docs)
with exactly one enforcement point. Adding a status meant finding all
fourteen; missing one produced a value the UI could not colour, a
schema that rejected a legal document, or a counter that silently
dropped a transition.

Everything Python-side now imports from here:

* :mod:`services.cowork_agent.visualizer.todos_store` — the HTTP
  API's validation gate (the only writer that rejects a bad status);
* :mod:`services.cowork_agent.visualizer.sinks.sessions_augment` —
  the per-session ``taskCount`` counters and their transition filter;
* :mod:`routers.cowork_agent.bff._visualizer_models` — the wire
  declaration (``TodoStatus``).

The non-Python copies cannot import this module, so they are held to
it by test instead: ``tests/test_todo_status.py`` asserts that both
JSON Schemas, the wire model, ``space_ui`` (JS sort order, done set,
dot colours, chip classes) and the agent-facing docs all enumerate
exactly :data:`TODO_STATUSES`. Change the tuple below and that test
names every file that still disagrees.

Semantics (§5.5 of the sync plan): ``status`` carries lifecycle only.
``cancelled`` means "we decided not to do this" — a real outcome that
stays visible. Deletion is a separate ``deleted_at`` tombstone, not a
sixth status.
"""

from __future__ import annotations

from typing import Final, Literal

#: Canonical order: the lifecycle reads pending → in_progress →
#: completed, with the two non-completing outcomes after it. This is
#: the order the JSON Schemas, the docs and the wire model use. It is
#: NOT the UI's display order — ``space_ui`` sorts in_progress first
#: because that is what a human wants at the top of a list.
TODO_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "in_progress",
    "completed",
    "cancelled",
    "blocked",
)

#: Membership test for validators and event filters.
VALID_TODO_STATUSES: Final[frozenset[str]] = frozenset(TODO_STATUSES)

#: The wire/type-level form of the same set, derived from the tuple so
#: the two can never drift.
TodoStatus = Literal[*TODO_STATUSES]
