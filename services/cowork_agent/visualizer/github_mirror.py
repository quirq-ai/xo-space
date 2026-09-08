"""``~/.quirq/projects/<pid>/github/issues.json`` — the GitHub issue mirror.

The runtime half of the workitems surface (``docs/workitems-plan.md`` §5.2).
:mod:`services.cowork_agent.connectors.github_issues` fetches one page; the
poller (:mod:`services.cowork_agent.github_poller`) decides *when* and *which
repo*; this module owns the document on disk and the merge rule that decides
what a page means. Nothing here makes a network call.

Four things are load-bearing.

**1. It is runtime tier, and the tier is enforced by construction.** Rule
R-TIER: derived, high-churn, re-fetchable state lives in ``~/.quirq/``; only
durable authored state lives in ``.xo/``. This document is re-fetched every
60 seconds and rebuilt from scratch after ``rm -rf ~/.quirq``, so a copy in
the synced tier would make ``.xo/`` churn at GitHub's rate — exactly what
syncplan T19/T20 spent their effort removing — and would hand A10 a document
that conflicts on every poll. So the path is resolved through
:func:`~services.cowork_agent.project_layout.runtime_dir_for_project` and this
module never names ``.xo`` at all. Note it deliberately does *not* use
``runtime_read_path``: that helper falls back to the pre-T19 in-project copy
for files that were *moved*, and this file never lived there — a read-through
would quietly legitimise an ``.xo/github/issues.json`` that no writer creates.

**2. The poller is the single writer, so the primitive is
:func:`~...atomic_write.write_json_atomic_if_changed`.** Full ownership is
what makes a corrupt file *repairable by overwriting* rather than something
to refuse: unlike ``workitems.json`` (O-E, where the bytes on disk may be the
only copy of authored state), losing this document costs exactly one poll.
:func:`~...flock.locked` still guards the read-modify-write, because a second
uvicorn worker is a second poller and two interleaved merges would drop a row
until the next tick.

**3. The merge rule is the fix for a real design bug** (§6.3, amendment 6).
With a high-water mark in ``filterBy.since`` and ``states: [OPEN]``, an issue
*closed* since the mark stops matching the query altogether — so an
incremental merge leaves a stale ``open`` row in the mirror **forever**, and
nothing ever corrects it. That is the "false statement about who owes what"
§5.3 forbids. Two mechanisms fix it together, and both live here:

* the steady-state poll asks for ``[OPEN, CLOSED]`` (the caller passes
  ``include_closed=True`` whenever it passes ``since``), so a transition is
  *observable*; and
* a **complete seed** — a poll with no ``since`` that consumed every page —
  **replaces** the issues map rather than merging into it, so any row that
  was stranded before this code existed is dropped the first time the mirror
  is reseeded.

**4. The high-water mark only advances on a complete poll.** The query is
``UPDATED_AT DESC``, so page 1 holds the newest rows. Advancing ``since`` to
the newest row seen while pages remain unread would skip everything in
between *permanently* — the same stranding class as the bug above, arrived at
from the other direction. So ``since`` moves only when the caller reports
``complete=True``, and it never moves backwards.

**Not here, on purpose.** The read-time projection that joins this document
with ``.xo/workitems.json`` is W7; adoption is W7; claims are W7b. This module
answers "what did GitHub last say", and nothing else.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from services.cowork_agent import project_layout
from services.cowork_agent.connectors.github_issues import IssuesResult, RateLimit
from services.cowork_agent.visualizer.atomic_write import write_json_atomic_if_changed
from services.cowork_agent.visualizer.flock import locked

logger = logging.getLogger(__name__)

#: On-disk revision of the mirror (plan §5.2).
MIRROR_SCHEMA = 1

#: Value stamped into ``$schema``. The schema's own ``$id``, not a filesystem
#: path — the ``.xo/schema/`` pointer syncplan T16 removed was dangling on
#: every document that carried it.
SCHEMA_REF = "xo/github-issues.schema.json"

#: The mirror's location *below* a project's runtime directory. Exported as a
#: constant so a reader joins the same relative path this writer does, and so
#: the tier decision stays in :mod:`project_layout` (T18's chokepoint rule).
MIRROR_SUBDIR = "github"
MIRROR_FILENAME = "issues.json"
MIRROR_RELATIVE = Path(MIRROR_SUBDIR) / MIRROR_FILENAME

#: How many ``closed`` rows the mirror retains.
#:
#: Closed rows are *kept*, not dropped: §5.3's projection reads an adopted
#: item's state from the mirror **always**, and "closed" is the true answer
#: the incremental poll just observed — dropping it would put the item back to
#: "unknown" one tick after we learned the truth. But an incremental merge
#: only ever adds, so an old, busy repo would accumulate every closure it ever
#: sees into a file rewritten once a minute. The cap bounds that, oldest
#: ``updated_at`` first. Losing an old closed row degrades an adopted item to
#: "stale, state unknown" — absent, which §5.3 says beats wrong — and never to
#: a false ``open``.
MAX_CLOSED_ROWS = 500

#: The keys an issue row may carry, matching ``github-issues.schema.json``'s
#: ``additionalProperties: false``. The client already emits exactly these,
#: but this document is validated against that schema and this module is the
#: one that writes it, so the guarantee is made here rather than borrowed.
_ROW_KEYS = (
    "node_id", "number", "title", "state", "state_reason",
    "assignees", "labels", "url", "updated_at",
)
_REQUIRED_ROW_KEYS = ("node_id", "number", "title", "state", "url", "updated_at")

_STATES = ("open", "closed")
_STATE_REASONS = ("completed", "not_planned", "reopened")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Where the document lives ─────────────────────────────────────────────────


def mirror_path(project: str, *, create: bool = False) -> Optional[Path]:
    """The mirror's path for one project, or ``None`` to skip.

    ``None`` means "there is nothing to resolve" — the project folder does not
    exist, or its pid is unusable as a path segment — and every caller treats
    that as an empty read or a skipped write, never as an error. ``create``
    makes the parent directory; a reader must never conjure one.
    """
    root = project_layout.runtime_dir_for_project(project, create=create)
    if root is None:
        return None
    target = root / MIRROR_RELATIVE
    if create:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("project %s: could not create %s", project, target.parent)
            return None
    return target


def read_mirror(project: str) -> Optional[dict]:
    """The mirror document, or ``None`` when it is absent or unusable.

    Unusable covers unreadable bytes, an empty file (what a truncated
    non-atomic write leaves behind), invalid JSON, a non-object, and a
    ``schema`` this revision does not write. All of them read as ``None``
    rather than raising, and the next successful poll overwrites the file —
    the deliberate opposite of ``workitems_store``'s refusal, because that
    document is authored state in the synced tier and this one is a cache
    whose loss costs one poll.
    """
    path = mirror_path(project)
    if path is None:
        return None
    return _read_document(path)


def _read_document(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("github mirror %s is unreadable (%s); it will be rebuilt", path, exc)
        return None
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("github mirror %s is not valid JSON (%s); it will be rebuilt", path, exc)
        return None
    if not isinstance(parsed, dict):
        return None
    version = parsed.get("schema")
    if isinstance(version, bool) or version != MIRROR_SCHEMA:
        logger.warning(
            "github mirror %s declares schema %r; this writer writes %d and "
            "will rebuild it", path, version, MIRROR_SCHEMA,
        )
        return None
    return parsed


# ── What the poller needs to know before it spends a point ───────────────────


@dataclass(frozen=True)
class MirrorState:
    """Everything the poller reads off the mirror before deciding a query.

    ``since`` is the whole point: ``None`` means "seed" — no ``since``
    variable and ``states: [OPEN]`` — and a value means "steady state", which
    the caller must pair with ``include_closed=True`` or it reintroduces the
    stranding bug this module exists to prevent.
    """

    path: Optional[Path] = None
    exists: bool = False
    repo: Optional[str] = None
    since: Optional[str] = None
    fetched_at: Optional[str] = None
    issue_count: int = 0
    error: Optional[dict] = None

    @property
    def seeded(self) -> bool:
        """Whether an incremental poll is safe. False on a fresh or reset mirror."""
        return bool(self.since)


def load_state(project: str, *, repo: Optional[str] = None) -> MirrorState:
    """Read the mirror's poll state for ``project``.

    ``repo`` is the slug the caller is *about* to poll. When it disagrees with
    the slug the document was written from — the project's remote changed —
    the state comes back unseeded, so the next poll seeds from scratch rather
    than merging one repository's issues into another's mirror. The schema
    records ``repo`` for exactly this reason.
    """
    path = mirror_path(project)
    if path is None:
        return MirrorState()
    doc = _read_document(path)
    if doc is None:
        return MirrorState(path=path, exists=False)

    stored_repo = doc.get("repo") if isinstance(doc.get("repo"), str) else None
    issues = doc.get("issues")
    count = len(issues) if isinstance(issues, dict) else 0
    if repo is not None and stored_repo is not None and stored_repo != repo:
        # A different repository's document. Report what is there, but never
        # hand back its high-water mark: it would filter the new repo's
        # issues by a timestamp that means nothing in it.
        return MirrorState(
            path=path, exists=True, repo=stored_repo, since=None,
            fetched_at=None, issue_count=count, error=_error_of(doc),
        )

    since = doc.get("since")
    fetched_at = doc.get("fetched_at")
    return MirrorState(
        path=path,
        exists=True,
        repo=stored_repo,
        since=since if isinstance(since, str) and since else None,
        fetched_at=fetched_at if isinstance(fetched_at, str) and fetched_at else None,
        issue_count=count,
        error=_error_of(doc),
    )


def _error_of(doc: dict) -> Optional[dict]:
    error = doc.get("error")
    return error if isinstance(error, dict) else None


# ── Rows ─────────────────────────────────────────────────────────────────────


def _clean_row(row: Any) -> Optional[dict]:
    """One row, reduced to the schema's declared keys, or ``None`` if unusable.

    Applied to rows read back off disk as well as to fresh ones. A row the
    mirror cannot vouch for is dropped rather than carried forward: the
    document is validated against ``github-issues.schema.json``, and a single
    poisoned row would make the whole file fail for every consumer.
    """
    if not isinstance(row, dict):
        return None
    out: dict[str, Any] = {}
    for key in _ROW_KEYS:
        if key in row:
            out[key] = row[key]
    for key in _REQUIRED_ROW_KEYS:
        if key not in out:
            return None
    if not isinstance(out["node_id"], str) or not out["node_id"]:
        return None
    number = out["number"]
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    if out["state"] not in _STATES:
        return None
    reason = out.get("state_reason")
    if reason is not None and reason not in _STATE_REASONS:
        out["state_reason"] = None
    for key in ("title", "url", "updated_at"):
        if not isinstance(out[key], str):
            return None
    assignees = out.get("assignees")
    if assignees is None:
        out.pop("assignees", None)
    elif isinstance(assignees, list):
        cleaned = []
        for person in assignees:
            if not isinstance(person, dict):
                continue
            login = person.get("login")
            if not isinstance(login, str) or not login:
                continue
            avatar = person.get("avatar_url")
            cleaned.append({
                "login": login,
                "avatar_url": avatar if isinstance(avatar, str) else None,
            })
        out["assignees"] = cleaned
    else:
        out.pop("assignees", None)
    labels = out.get("labels")
    if labels is not None and not (
        isinstance(labels, list) and all(isinstance(item, str) for item in labels)
    ):
        # Absent, never ``[]``: an empty array asserts "this issue has no
        # labels", which the poll never establishes (§5.2, amendment 2).
        out.pop("labels", None)
    return out


def _clean_rows(rows: Any) -> dict[str, dict]:
    if not isinstance(rows, dict):
        return {}
    out: dict[str, dict] = {}
    for key, row in rows.items():
        cleaned = _clean_row(row)
        if cleaned is None or not isinstance(key, str):
            continue
        if cleaned["node_id"] != key:
            # The O-C lesson: never *derive* identity from a key. The row
            # carries its own node_id; if the two disagree the pair is not
            # trustworthy and neither half is preferred.
            continue
        out[key] = cleaned
    return out


def _prune_closed(issues: dict[str, dict], cap: int = MAX_CLOSED_ROWS) -> dict[str, dict]:
    """Bound the closed rows, oldest ``updated_at`` first. Open rows are never
    dropped — they are the answer to "who owes what"."""
    closed = [row for row in issues.values() if row.get("state") == "closed"]
    if len(closed) <= max(0, cap):
        return issues
    closed.sort(key=lambda row: (str(row.get("updated_at") or ""), str(row.get("node_id"))))
    drop = {row["node_id"] for row in closed[: len(closed) - max(0, cap)]}
    return {key: row for key, row in issues.items() if key not in drop}


def _high_water(rows: Iterable[dict]) -> Optional[str]:
    stamps = [str(row.get("updated_at")) for row in rows if row.get("updated_at")]
    return max(stamps) if stamps else None


# ── Writing ──────────────────────────────────────────────────────────────────


def _rate_document(pages: Sequence[IssuesResult]) -> Optional[dict]:
    """``rate`` for the document: the newest reading, with this poll's total cost.

    ``remaining``/``reset_at``/``limit`` come from the last page that reported
    them — the most recent truth GitHub told us — while ``cost`` is summed
    across the pages, because "the last poll" is the whole multi-page poll and
    a per-page 1 would understate what a 250-issue repo actually spends.
    ``None`` when nothing was observed: a fabricated budget is how a poller
    talks itself past a limit it has really hit.
    """
    latest: Optional[RateLimit] = None
    total = 0
    seen_cost = False
    for page in pages:
        rate = page.rate
        if rate.cost is not None:
            total += rate.cost
            seen_cost = True
        if rate.known:
            latest = rate
    if latest is None:
        return None
    doc: dict[str, Any] = {"remaining": latest.remaining, "reset_at": latest.reset_at}
    if latest.limit is not None:
        doc["limit"] = latest.limit
    if seen_cost:
        doc["cost"] = total
    return doc


def _document(
    *,
    repo: str,
    fetched_at: Optional[str],
    since: Optional[str],
    rate: Optional[dict],
    error: Optional[dict],
    issues: dict[str, dict],
) -> dict:
    """The §5.2 document, in the schema's key order.

    ``etag`` is not written. It was a REST idea that does not survive D5 —
    GraphQL has no conditional request, so there is no 304-style free poll on
    this path (§13 amendment 3). The schema still declares it so the plan's
    literal example validates.
    """
    return {
        "$schema": SCHEMA_REF,
        "schema": MIRROR_SCHEMA,
        "repo": repo,
        "fetched_at": fetched_at,
        "since": since,
        "rate": rate,
        "error": error,
        "issues": dict(sorted(issues.items())),
    }


def _write(path: Path, payload: dict) -> bool:
    """Persist, skipping a write that would only restamp a repeated failure.

    ``error.at`` is the sole volatile path. A poll that succeeds always
    changes ``fetched_at``, which is what the UI's staleness indicator reads,
    so a success always writes — that is the point of the file. But a machine
    with no GitHub auth fails identically every 60 seconds forever, and
    without this mask each of those identical failures would rewrite the
    document just to move a timestamp nobody is waiting on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_json_atomic_if_changed(path, payload, ("error.at",))


def record_pages(
    project: str,
    *,
    repo: str,
    pages: Sequence[IssuesResult],
    complete: bool,
) -> bool:
    """Fold one poll's pages into the mirror. Returns ``True`` iff it wrote.

    :param pages: the **successful** :class:`IssuesResult` pages of one poll,
        in fetch order. An empty sequence is legitimate (a steady-state poll
        of a quiet repo returns no rows) and still refreshes ``fetched_at``.
    :param complete: whether the poll consumed every page GitHub offered.

    The merge, in one paragraph. A poll with no stored ``since`` is a **seed**
    and its result is the repository's whole open set; when it completed, the
    issues map is **replaced**, which is what drops a row stranded by an
    earlier ``[OPEN]``-only incremental poll and what makes ``rm -rf ~/.quirq``
    a real repair. An incomplete seed **merges** instead — a partial snapshot
    is not authoritative about what is missing, and replacing would make two
    successive truncated seeds oscillate instead of accumulate. Every other
    poll merges by ``node_id``, so a row that comes back ``closed`` overwrites
    the stale ``open`` one rather than sitting beside it.

    ``since`` advances only when ``complete``, and never backwards.
    """
    path = mirror_path(project, create=True)
    if path is None:
        return False

    fetched_at = _utc_now()
    fresh: dict[str, dict] = {}
    for page in pages:
        for node_id, row in page.issues_by_node_id().items():
            cleaned = _clean_row(row)
            if cleaned is not None and cleaned["node_id"] == node_id:
                fresh[node_id] = cleaned

    with locked(path):
        previous = _read_document(path) or {}
        stored_repo = previous.get("repo")
        same_repo = stored_repo == repo
        stored_since = previous.get("since") if same_repo else None
        stored_since = stored_since if isinstance(stored_since, str) and stored_since else None
        seeding = stored_since is None

        if seeding and complete:
            issues = fresh
        else:
            issues = _clean_rows(previous.get("issues")) if same_repo else {}
            issues.update(fresh)
        issues = _prune_closed(issues)

        since = stored_since
        if complete:
            mark = _high_water(fresh.values())
            if mark and (since is None or mark > since):
                since = mark

        payload = _document(
            repo=repo,
            fetched_at=fetched_at,
            since=since,
            rate=_rate_document(pages),
            error=None,
            issues=issues,
        )
        return _write(path, payload)


def record_failure(project: str, *, repo: Optional[str], result: IssuesResult) -> bool:
    """Record a failed poll without losing the last good mirror.

    ``fetched_at`` deliberately does **not** move: it means "when the poll
    that produced these issues completed", and a failure produced none. The
    failure carries its own ``error.at``, so the UI can say both "last
    refreshed 09:00" and "tried 09:05, GitHub unreachable" — two different
    facts that a single timestamp would collapse into a lie.

    A failed poll is not evidence that the issues went away, so the rows are
    carried through untouched. When there is no document yet the file is
    created with an empty issue map and ``fetched_at: null``, which the schema
    declares for exactly this state — a UI needs something to hang "connect
    GitHub" off before the first successful poll.
    """
    slug = repo or result.repo
    if not slug:
        return False
    path = mirror_path(project, create=True)
    if path is None:
        return False

    error = result.error_document() or {
        "kind": "unknown",
        "message": "GitHub poll failed.",
        "at": _utc_now(),
    }

    with locked(path):
        previous = _read_document(path) or {}
        same_repo = previous.get("repo") == slug
        issues = _clean_rows(previous.get("issues")) if same_repo else {}
        stored_since = previous.get("since") if same_repo else None
        stored_fetched = previous.get("fetched_at") if same_repo else None
        rate = _rate_document([result])
        if rate is None and same_repo and isinstance(previous.get("rate"), dict):
            # A failure that never reached GitHub (no gh, no network) knows
            # nothing about the budget. Keeping the last observed reading is
            # honest; replacing it with null would read as "budget unknown"
            # when in fact it is merely unchanged.
            rate = previous["rate"]

        payload = _document(
            repo=slug,
            fetched_at=stored_fetched if isinstance(stored_fetched, str) else None,
            since=stored_since if isinstance(stored_since, str) and stored_since else None,
            rate=rate,
            error=error,
            issues=issues,
        )
        return _write(path, payload)


def reset_mirror(project: str) -> bool:
    """Delete the mirror. ``True`` iff a file was removed.

    The escape hatch for the one state this module cannot merge its way out
    of: a document whose rows are wrong in a way a merge cannot see. The next
    poll seeds from scratch, which costs one point. Not called by the poller
    — it is here because ``rm -rf ~/.quirq`` is a documented repair and a
    single project deserves the same repair without the blast radius.
    """
    path = mirror_path(project)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("could not reset github mirror %s: %s", path, exc)
        return False
