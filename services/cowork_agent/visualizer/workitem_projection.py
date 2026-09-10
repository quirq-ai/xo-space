"""The read-time projection: `.xo/workitems.json` joined with the mirror."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any, Optional

from services.cowork_agent.visualizer.workitems_store import (
    VALID_STATE_REASONS,
    VALID_STATUSES,
    is_adopted,
    is_deleted,
)

logger = logging.getLogger(__name__)

#: The keys the projection adds to a stored record on its way to the wire.
PROJECTED_KEYS: tuple[str, ...] = ("assignees", "github_assignees", "stale")


def mirror_issues(mirror: object) -> dict[str, dict]:
    """The ``issues`` map of a mirror document, or ``{}``."""
    if not isinstance(mirror, Mapping):
        return {}
    issues = mirror.get("issues")
    if not isinstance(issues, Mapping):
        return {}
    return {
        str(key): dict(row)
        for key, row in issues.items()
        if isinstance(row, Mapping)
    }


def adopted_node_id(record: object) -> Optional[str]:
    """The ``node_id`` a record is adopted to, or ``None``."""
    if not is_adopted(record):
        return None
    try:
        ref = record["source"].get("github")  # type: ignore[index]
    except Exception:
        return None
    if not isinstance(ref, Mapping):
        return None
    node_id = ref.get("node_id")
    return node_id if isinstance(node_id, str) and node_id else None


def tracked_node_ids(records: Iterable[Any]) -> dict[str, str]:
    """``node_id`` → workitem id, for every live adopted record."""
    out: dict[str, str] = {}
    for record in records or []:
        if not isinstance(record, Mapping) or is_deleted(record):
            continue
        node_id = adopted_node_id(record)
        if node_id and isinstance(record.get("id"), str):
            out[node_id] = str(record["id"])
    return out


def _logins(row: Mapping) -> list[str]:
    """A mirror row's ``assignees[].login``, flattened."""
    people = row.get("assignees")
    if not isinstance(people, list):
        return []
    out: list[str] = []
    for person in people:
        if isinstance(person, Mapping):
            login = person.get("login")
        else:
            login = person
        if isinstance(login, str) and login and login not in out:
            out.append(login)
    return out


def _choice(value: object, allowed: frozenset[str]) -> Optional[str]:
    """A mirror enum, or ``None`` when it is not one this Space knows."""
    return value if isinstance(value, str) and value in allowed else None


def project_workitem(
    record: Mapping, *, issues: Optional[Mapping[str, Mapping]] = None
) -> dict:
    """One stored record, joined with the mirror. **Never raises.**"""
    try:
        return _project(record, issues or {})
    except Exception:  # pragma: no cover - defensive; the join must be total
        logger.warning(
            "could not project workitem %r against the GitHub mirror; "
            "serving the file's own view of it",
            record.get("id") if isinstance(record, Mapping) else None,
            exc_info=True,
        )
        out = dict(record) if isinstance(record, Mapping) else {}
        assignee = out.get("assignee")
        out.setdefault(
            "assignees",
            [assignee] if isinstance(assignee, str) and assignee else [],
        )
        out.setdefault("github_assignees", [])
        out.setdefault("stale", False)
        return out


def _project(record: Mapping, issues: Mapping[str, Mapping]) -> dict:
    if not isinstance(record, Mapping):
        # A known shape to degrade on rather than an unexpected failure: the
        # store only ever hands back dicts, but a caller passing something else
        # deserves an empty projection, not a stack trace in the log of a
        # request that succeeded anyway.
        return {"assignees": [], "github_assignees": [], "stale": False}
    out = dict(record)
    # Assignment is ours for both kinds (amendment 33), so it is read off the
    # file *before* the branch and never touched again below.
    assignee = out.get("assignee")
    assignee = assignee if isinstance(assignee, str) and assignee else None
    out["assignee"] = assignee
    out["assignees"] = [assignee] if assignee else []
    node_id = adopted_node_id(record)
    if node_id is None:
        # A local item answers for itself, and no issue anywhere has an opinion
        # about it — an empty list rather than an absent key, so the two kinds
        # serialise the same shape.
        out["github_assignees"] = []
        out["stale"] = False
        return out

    row = issues.get(node_id) if isinstance(issues, Mapping) else None
    if not isinstance(row, Mapping):
        # The §5.3 promise, and the acceptance criterion for W7: readable,
        # flagged, and honest about what it does not know. Never a 404.
        out["stale"] = True
        out["status"] = None
        out["state_reason"] = None
        out["github_assignees"] = []
        out["body"] = None
        return out

    title = row.get("title")
    if isinstance(title, str) and title.strip():
        out["title"] = title
    labels = row.get("labels")
    if isinstance(labels, list) and all(isinstance(x, str) for x in labels):
        # The poller never writes this key — a nested ``labels`` connection was
        # measured to double the query's cost and halve the project ceiling
        # (§6.2) — so in practice the adoption snapshot below is what answers.
        out["labels"] = labels
    out["status"] = _choice(row.get("state"), VALID_STATUSES)
    out["state_reason"] = _choice(row.get("state_reason"), VALID_STATE_REASONS)
    # Information, not assignment.
    out["github_assignees"] = _logins(row)
    # The mirror carries no body (§5.2's query does not select one), so this is
    # unknown rather than empty. The issue URL is where it is read.
    out["body"] = None
    out["stale"] = False
    return out


def project_workitems(
    records: Iterable[Mapping], *, issues: Optional[Mapping[str, Mapping]] = None
) -> list[dict]:
    """:func:`project_workitem` over a list, in the order given."""
    joined = issues or {}
    return [project_workitem(record, issues=joined) for record in records or []]
