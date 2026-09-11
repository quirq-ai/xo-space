"""The todo lifecycle status vocabulary — one definition, one place."""

from __future__ import annotations

from typing import Final, Literal

#: Canonical order: the lifecycle reads pending → in_progress → completed, with
#: the two non-completing outcomes after it.
TODO_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "in_progress",
    "completed",
    "cancelled",
    "blocked",
)

#: Membership test for validators and event filters.
VALID_TODO_STATUSES: Final[frozenset[str]] = frozenset(TODO_STATUSES)

#: The wire/type-level form of the same set, derived from the tuple so the two
#: can never drift.
TodoStatus = Literal[*TODO_STATUSES]
