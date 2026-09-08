"""The read-time projection: `.xo/workitems.json` joined with the mirror.

``docs/workitems-plan.md`` §5.3, task W7. Two documents hold one workitem
between them, and this module is the join rule that decides which of them
answers each field:

===================  =========================  =============================
field                ``source.kind == local``   ``source.kind == github``
===================  =========================  =============================
``title``            the file                   the mirror; the file's
                                                adoption snapshot when the
                                                mirror is stale or absent
``labels``           the file                   the file's snapshot — the
                                                poll never fetches labels
``body``             the file                   nobody (see below)
``assignee``         **the file**               **the file** (amendment 33)
``github_assignees`` always ``[]``              the mirror's own assignees,
                                                as information only
``status``           the file                   **the mirror, always**
``in_progress``      a live claim (§5.4)        a live claim (§5.4)
``links``            the file                   the file
===================  =========================  =============================

**``assignee`` changed sides, and that is the point of amendment 33.** It
used to read "the mirror, always" for an adopted item, because D1 put
coordination in GitHub and ``PUT …/assignee`` wrote there. GitHub is now
read-only to this system: assignment is a local annotation stored in
``.xo/workitems.json`` for *every* workitem, so the file answers for both
kinds and there is no join to do. What the mirror knows is still served,
under a name that cannot be mistaken for ours — ``github_assignees``, "who
GitHub thinks is on this issue". Keeping the two under one name would make a
row that says ``assignee: "ada"`` ambiguous about who decided that, which is
the whole reason the field exists.

Three things about it are load-bearing.

**1. It is total. It cannot raise, ever.** The BFF builds its wire models
*outside* the route's ``try``/``except`` — deliberately, because a
``ValidationError`` raised there once took an entire project's list down with
it (the ``_coerce_status`` defect, R1). A projection that could throw would
put that failure straight back, one layer lower and harder to see. So every
entry point below degrades to the file's own view of the record rather than
propagating: the worst outcome is a row that reads as stale, which is exactly
what a row with no mirror reads as anyway.

**2. A missing issue is never a 404.** An adopted item whose issue is absent
from the mirror — deleted, transferred, the poller has never run, ``gh`` is
not installed, the machine is offline — still renders. It renders as the
title and labels snapshotted at adoption plus the issue reference, flagged
:data:`stale`, with GitHub's half **unknown**: ``status``, ``state_reason``
and ``body`` come back ``None``, not defaulted, and ``github_assignees`` is
empty because the mirror asserts nothing. ``assignee`` is unaffected — it
was never GitHub's to lose, so a stale row still says who this Space put on
it. That is §5.3's rule that absent beats wrong, applied at the only place
that could break it. The feature has to be useful with GitHub switched off.

**3. ``body`` is unknown for an adopted item, and the plan is wrong about
why.** §5.3 says the body comes from "the mirror only — not snapshotted", but
§5.2's mirror carries no body at all: the pinned poll query does not select
one, because payload per repo per minute is the thing that query exists to
keep small. So an adopted item's ``body`` is ``None`` — unknown, with the
issue URL as the place to read it — and it stays that way until something
fetches bodies. Recorded here rather than silently implemented, because the
two sections of the plan disagree and a reader deserves to know which one the
code follows.

Nothing here reads a file or makes a network call: it takes the parsed
records and the parsed mirror and returns dicts. That is what makes the join
rule testable on its own, and what keeps this module out of the tier
question entirely — the file it joins are resolved by their own owners.
"""

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
#: None of them is stored anywhere: ``stale`` is a statement about the
#: *mirror*, ``assignees`` is this Space's own single ``assignee`` as a list,
#: and ``github_assignees`` is the mirror's own list flattened to logins —
#: information about the issue, never an assignment this system made.
PROJECTED_KEYS: tuple[str, ...] = ("assignees", "github_assignees", "stale")


def mirror_issues(mirror: object) -> dict[str, dict]:
    """The ``issues`` map of a mirror document, or ``{}``.

    ``{}`` for every unusable shape — absent document, wrong type, a missing
    or malformed map. A mirror that cannot be read means "GitHub has told us
    nothing", which is the same state as a poller that has never run, and
    both must project as stale rather than as an error.
    """
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
    """The ``node_id`` a record is adopted to, or ``None``.

    The match key is the node id and never ``repo``/``number``: a repository
    rename or transfer changes those two and leaves the node id alone (§5.1),
    so matching on them would strand every adopted item the day a repo moves.
    """
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
    """``node_id`` → workitem id, for every live adopted record.

    The inverse of the join, and what ``GET /github/issues`` uses to say
    which issues are already tracked and how many are not.
    """
    out: dict[str, str] = {}
    for record in records or []:
        if not isinstance(record, Mapping) or is_deleted(record):
            continue
        node_id = adopted_node_id(record)
        if node_id and isinstance(record.get("id"), str):
            out[node_id] = str(record["id"])
    return out


def _logins(row: Mapping) -> list[str]:
    """A mirror row's ``assignees[].login``, flattened.

    Kept as a list because it *is* one: an issue can carry several
    assignees, and collapsing them to the first would make the surface
    disagree with GitHub about who it has on the issue. It is served as
    ``github_assignees`` and is **not** this system's assignment — see the
    module docstring.
    """
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
    """A mirror enum, or ``None`` when it is not one this Space knows.

    ``None`` rather than a nearest neighbour: GitHub has added state reasons
    since §5.4 was written, and mapping an unrecognised one onto
    ``not_planned`` would put a claim in the projection that GitHub never
    made. The same rule the client applies at the other end of the pipe.
    """
    return value if isinstance(value, str) and value in allowed else None


def project_workitem(
    record: Mapping, *, issues: Optional[Mapping[str, Mapping]] = None
) -> dict:
    """One stored record, joined with the mirror. **Never raises.**

    Returns a new dict — the stored fields with the §5.3 join applied, plus
    ``assignees`` (this Space's own assignment, as a list of at most one),
    ``github_assignees`` (the mirror's list, flattened to logins — GitHub's
    view of the issue, not an assignment we made) and ``stale``.

    ``stale`` is precisely "this record is adopted and its issue is not in
    the mirror". It is not a freshness measure: how *old* the mirror is is a
    property of the whole document (``fetched_at``, and the ``error`` beside
    it), served by ``GET /github/issues``, and conflating the two would let a
    single stale flag mean two different things to a UI. A local workitem is
    never stale — nothing external owns any part of it.
    """
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
        # store only ever hands back dicts, but a caller passing something
        # else deserves an empty projection, not a stack trace in the log of
        # a request that succeeded anyway.
        return {"assignees": [], "github_assignees": [], "stale": False}
    out = dict(record)
    # Assignment is ours for both kinds (amendment 33), so it is read off the
    # file *before* the branch and never touched again below. ``assignees``
    # is the same fact as a list, kept so a client reads one shape whether or
    # not it cares about the singular field.
    assignee = out.get("assignee")
    assignee = assignee if isinstance(assignee, str) and assignee else None
    out["assignee"] = assignee
    out["assignees"] = [assignee] if assignee else []
    node_id = adopted_node_id(record)
    if node_id is None:
        # A local item answers for itself, and no issue anywhere has an
        # opinion about it — an empty list rather than an absent key, so the
        # two kinds serialise the same shape.
        out["github_assignees"] = []
        out["stale"] = False
        return out

    row = issues.get(node_id) if isinstance(issues, Mapping) else None
    if not isinstance(row, Mapping):
        # The §5.3 promise, and the acceptance criterion for W7: readable,
        # flagged, and honest about what it does not know. Never a 404.
        # ``assignee`` survives untouched: the mirror never owned it, so
        # there is nothing about it that the mirror's absence makes unknown.
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
        # The poller never writes this key — a nested ``labels`` connection
        # was measured to double the query's cost and halve the project
        # ceiling (§6.2) — so in practice the adoption snapshot below is what
        # answers. It is read anyway, because "absent" is the claim the
        # mirror makes and a future writer that does fetch them should win.
        out["labels"] = labels
    out["status"] = _choice(row.get("state"), VALID_STATUSES)
    out["state_reason"] = _choice(row.get("state_reason"), VALID_STATE_REASONS)
    # Information, not assignment. GitHub's assignees are still worth seeing
    # — they are how a peer says "I have this" in the place peers can see —
    # but nothing in this system writes them and they never overwrite
    # ``assignee``, which is what this Space decided.
    out["github_assignees"] = _logins(row)
    # The mirror carries no body (§5.2's query does not select one), so this
    # is unknown rather than empty. The issue URL is where it is read.
    out["body"] = None
    out["stale"] = False
    return out


def project_workitems(
    records: Iterable[Mapping], *, issues: Optional[Mapping[str, Mapping]] = None
) -> list[dict]:
    """:func:`project_workitem` over a list, in the order given."""
    joined = issues or {}
    return [project_workitem(record, issues=joined) for record in records or []]
