"""``~/.quirq/projects/<pid>/github/issues.json`` — the GitHub issue and pull request mirror."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from services.cowork_agent import project_layout
from services.cowork_agent.connectors.github.issues import IssuesResult, RateLimit
from services.cowork_agent.visualizer.atomic_write import write_json_atomic_if_changed
from services.cowork_agent.visualizer.flock import locked
from utils.safe_read import read_text_guarded

logger = logging.getLogger(__name__)

#: On-disk revision of the mirror (plan §5.2).
MIRROR_SCHEMA = 1

#: Value stamped into ``$schema``.
SCHEMA_REF = "xo/github-issues.schema.json"

#: The mirror's location *below* a project's runtime directory.
MIRROR_SUBDIR = "github"
MIRROR_FILENAME = "issues.json"
MIRROR_RELATIVE = Path(MIRROR_SUBDIR) / MIRROR_FILENAME

#: How many ``closed`` rows the mirror retains.
MAX_CLOSED_ROWS = 500

#: The keys an issue row may carry, matching ``github-issues.schema.json``'s
#: ``additionalProperties: false``.
_ROW_KEYS = (
    "node_id", "number", "title", "state", "state_reason",
    "assignees", "labels", "url", "updated_at", "author", "comment_count",
)

#: The keys a pull request row may carry (``definitions.pull`` in the schema).
_PULL_KEYS = (
    "node_id", "number", "title", "state", "is_draft", "url", "updated_at",
    "author", "head", "base", "head_oid", "base_oid", "checks",
    "review_decision", "mergeable", "additions", "deletions", "changed_files",
    "comment_count",
)
_PULL_STATES = ("open", "closed", "merged")
_PULL_ENUMS = {
    "checks": ("SUCCESS", "FAILURE", "ERROR", "PENDING", "EXPECTED"),
    "review_decision": ("REVIEW_REQUIRED", "CHANGES_REQUESTED", "APPROVED"),
}
_MERGEABLE = ("MERGEABLE", "CONFLICTING", "UNKNOWN")
_REQUIRED_ROW_KEYS = ("node_id", "number", "title", "state", "url", "updated_at")

_STATES = ("open", "closed")
_STATE_REASONS = ("completed", "not_planned", "reopened")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Where the document lives ─────────────────────────────────────────────────


def mirror_path(project: str, *, create: bool = False) -> Optional[Path]:
    """The mirror's path for one project, or ``None`` to skip."""
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
    """The mirror document, or ``None`` when it is absent or unusable."""
    path = mirror_path(project)
    if path is None:
        return None
    return _read_document(path)


def _read_document(path: Path) -> Optional[dict]:
    try:
        text = read_text_guarded(path)
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
    """Everything the poller reads off the mirror before deciding a query."""

    path: Optional[Path] = None
    exists: bool = False
    repo: Optional[str] = None
    since: Optional[str] = None
    fetched_at: Optional[str] = None
    issue_count: int = 0
    error: Optional[dict] = None
    #: An open row predates the author/comment/label fields. An incremental
    #: poll only rewrites the issues that changed, so such a row would lack
    #: them until someone touched it: one full read of the open issues
    #: refills them (see ``poll_project``).
    needs_refill: bool = False

    @property
    def seeded(self) -> bool:
        """Whether an incremental poll is safe. False on a fresh or reset mirror."""
        return bool(self.since)


def load_state(project: str, *, repo: Optional[str] = None) -> MirrorState:
    """Read the mirror's poll state for ``project``."""
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
        # A different repository's document.
        return MirrorState(
            path=path, exists=True, repo=stored_repo, since=None,
            fetched_at=None, issue_count=count, error=_error_of(doc),
        )

    since = doc.get("since")
    fetched_at = doc.get("fetched_at")
    refill = isinstance(issues, dict) and any(
        isinstance(row, dict) and row.get("state") == "open" and "comment_count" not in row
        for row in issues.values()
    )
    return MirrorState(
        path=path,
        exists=True,
        repo=stored_repo,
        since=since if isinstance(since, str) and since else None,
        fetched_at=fetched_at if isinstance(fetched_at, str) and fetched_at else None,
        issue_count=count,
        error=_error_of(doc),
        needs_refill=refill,
    )


def _error_of(doc: dict) -> Optional[dict]:
    error = doc.get("error")
    return error if isinstance(error, dict) else None


# ── Rows ─────────────────────────────────────────────────────────────────────


def _clean_row(row: Any) -> Optional[dict]:
    """One row, reduced to the schema's declared keys, or ``None`` if unusable."""
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
    _clean_person(out, "author")
    _clean_count(out, "comment_count")
    labels = out.get("labels")
    if labels is not None and not (
        isinstance(labels, list) and all(isinstance(item, str) for item in labels)
    ):
        # Absent, never ``[]``: an empty array asserts "this issue has no
        # labels", which the poll never establishes (§5.2, amendment 2).
        out.pop("labels", None)
    return out


def _clean_person(row: dict, key: str) -> None:
    """Keep ``row[key]`` only as ``{login, avatar_url}`` or ``None``."""
    value = row.get(key)
    if key not in row or value is None:
        return
    login = value.get("login") if isinstance(value, dict) else None
    if not isinstance(login, str) or not login:
        row.pop(key, None)
        return
    avatar = value.get("avatar_url")
    row[key] = {"login": login, "avatar_url": avatar if isinstance(avatar, str) else None}


def _clean_count(row: dict, key: str) -> None:
    """Keep ``row[key]`` only as a non-negative int or ``None``."""
    value = row.get(key)
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        row.pop(key, None)


def _clean_pull(row: Any) -> Optional[dict]:
    """One pull request row, reduced to the schema's keys, or ``None``."""
    if not isinstance(row, dict):
        return None
    out = {key: row[key] for key in _PULL_KEYS if key in row}
    node_id, number = out.get("node_id"), out.get("number")
    if not isinstance(node_id, str) or not node_id:
        return None
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return None
    if out.get("state") not in _PULL_STATES:
        return None
    for key in ("title", "url", "updated_at"):
        if not isinstance(out.get(key), str):
            return None
    out["is_draft"] = out.get("is_draft") is True
    for key in ("head", "base", "head_oid", "base_oid"):
        if key in out and not (isinstance(out[key], str) and out[key]):
            out[key] = None
    for key, allowed in _PULL_ENUMS.items():
        if out.get(key) not in allowed:
            out[key] = None
    if out.get("mergeable") not in _MERGEABLE:
        out["mergeable"] = "UNKNOWN"
    _clean_person(out, "author")
    for key in ("additions", "deletions", "changed_files", "comment_count"):
        _clean_count(out, key)
    return out


def _clean_pulls(rows: Any) -> Optional[dict[str, dict]]:
    """The ``pulls`` map, or ``None`` when the document has none."""
    if not isinstance(rows, dict):
        return None
    out: dict[str, dict] = {}
    for key, row in rows.items():
        cleaned = _clean_pull(row)
        if cleaned is not None and cleaned["node_id"] == key:
            out[key] = cleaned
    return out


def _settle_mergeable(fresh: dict[str, dict], previous: Optional[dict[str, dict]]) -> None:
    """Keep a known merge state through GitHub's lazy ``UNKNOWN``.

    GitHub computes ``mergeable`` in the background and answers ``UNKNOWN``
    until it has. When neither the head nor the base has moved since the
    last poll, the last known answer is still the answer, and keeping it
    stops a row from flickering between "conflicts" and nothing.
    """
    for node_id, row in fresh.items():
        if row.get("mergeable") != "UNKNOWN" or row.get("state") != "open":
            continue
        before = (previous or {}).get(node_id)
        if not before or before.get("mergeable") not in ("MERGEABLE", "CONFLICTING"):
            continue
        if (row.get("head_oid") and row.get("head_oid") == before.get("head_oid")
                and row.get("base_oid") and row.get("base_oid") == before.get("base_oid")):
            row["mergeable"] = before["mergeable"]


def _clean_rows(rows: Any) -> dict[str, dict]:
    if not isinstance(rows, dict):
        return {}
    out: dict[str, dict] = {}
    for key, row in rows.items():
        cleaned = _clean_row(row)
        if cleaned is None or not isinstance(key, str):
            continue
        if cleaned["node_id"] != key:
            # The O-C lesson: never *derive* identity from a key.
            continue
        out[key] = cleaned
    return out


def _prune_closed(issues: dict[str, dict], cap: int = MAX_CLOSED_ROWS) -> dict[str, dict]:
    """
    Bound the closed rows, oldest ``updated_at`` first. Open rows are never
    dropped — they are the answer to "who owes what".
    """
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
    """``rate`` for the document: the newest reading, with this poll's total cost."""
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
    issues_enabled: Optional[bool] = None,
    pulls: Optional[dict[str, dict]] = None,
    open_pulls_total: Optional[int] = None,
) -> dict:
    """The §5.2 document, in the schema's key order."""
    return {
        "$schema": SCHEMA_REF,
        "schema": MIRROR_SCHEMA,
        "repo": repo,
        "fetched_at": fetched_at,
        "since": since,
        "rate": rate,
        "error": error,
        # Whether the repository has its issue tracker turned on (issuesplan
        # I4).
        "issues_enabled": issues_enabled,
        "issues": dict(sorted(issues.items())),
        # The pull requests as the newest first page read them: open ones (up
        # to ``OPEN_PULLS_PAGE``) and the latest merged or closed. A snapshot,
        # replaced whole on each poll; ``None`` until a poll has read them.
        "pulls": dict(sorted(pulls.items())) if pulls is not None else None,
        "open_pulls_total": open_pulls_total,
    }


def _write(path: Path, payload: dict) -> bool:
    """Persist, skipping a write that would only restamp a repeated failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_json_atomic_if_changed(path, payload, ("error.at",))


def _carried_total(previous: dict) -> Optional[int]:
    value = previous.get("open_pulls_total")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def mirror_pulls(mirror: object) -> Optional[dict[str, dict]]:
    """The ``pulls`` map of a mirror document, cleaned; ``None`` when no
    poll has read the pull requests yet."""
    return _clean_pulls(mirror.get("pulls")) if isinstance(mirror, dict) else None


def record_pages(
    project: str,
    *,
    repo: str,
    pages: Sequence[IssuesResult],
    complete: bool,
    advance_since: bool = True,
) -> bool:
    """Fold one poll's pages into the mirror. Returns ``True`` iff it wrote.

    ``advance_since=False`` is for a refill: a read of the open issues only,
    which says nothing about the issues closed since the stored mark, so the
    mark stays where it was and the next regular poll still sees them.
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
        if complete and (advance_since or since is None):
            mark = _high_water(fresh.values())
            if mark and (since is None or mark > since):
                since = mark

        # From the newest page that answered — every page of one poll reports
        # the same repository setting, and a poll that fetched no page at all
        # leaves it unknown rather than guessing.
        enabled: Optional[bool] = None
        for page in pages:
            if page.issues_enabled is not None:
                enabled = page.issues_enabled
        if enabled is None and isinstance(previous.get("issues_enabled"), bool):
            # Nothing new to say: keep what the last poll established rather
            # than downgrading a known answer to unknown.
            enabled = bool(previous["issues_enabled"])

        old_pulls = _clean_pulls(previous.get("pulls")) if same_repo else None
        pulls, open_total = old_pulls, _carried_total(previous) if same_repo else None
        for page in pages:
            if page.pulls is not None:
                pulls = {}
                for row in page.pulls:
                    cleaned = _clean_pull(row)
                    if cleaned is not None:
                        pulls[cleaned["node_id"]] = cleaned
                _settle_mergeable(pulls, old_pulls)
                open_total = page.open_pulls_total

        payload = _document(
            repo=repo,
            fetched_at=fetched_at,
            since=since,
            rate=_rate_document(pages),
            error=None,
            issues=issues,
            issues_enabled=enabled,
            pulls=pulls,
            open_pulls_total=open_total,
        )
        return _write(path, payload)


def record_failure(project: str, *, repo: Optional[str], result: IssuesResult) -> bool:
    """Record a failed poll without losing the last good mirror."""
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
            # nothing about the budget.
            rate = previous["rate"]

        payload = _document(
            repo=slug,
            fetched_at=stored_fetched if isinstance(stored_fetched, str) else None,
            since=stored_since if isinstance(stored_since, str) and stored_since else None,
            rate=rate,
            error=error,
            issues=issues,
            # A failed poll establishes nothing about the repository's
            # settings, so the last known answer is carried through for the
            # same reason ``rate`` and the rows are: unchanged, not unknown.
            issues_enabled=(
                bool(previous["issues_enabled"])
                if same_repo and isinstance(previous.get("issues_enabled"), bool)
                else None
            ),
            pulls=_clean_pulls(previous.get("pulls")) if same_repo else None,
            open_pulls_total=_carried_total(previous) if same_repo else None,
        )
        return _write(path, payload)


def reset_mirror(project: str) -> bool:
    """Delete the mirror. ``True`` iff a file was removed."""
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
