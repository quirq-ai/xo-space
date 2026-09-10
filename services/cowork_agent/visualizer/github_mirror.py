"""``~/.quirq/projects/<pid>/github/issues.json`` — the GitHub issue mirror."""

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
    "assignees", "labels", "url", "updated_at",
)
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
    """Everything the poller reads off the mirror before deciding a query."""

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
    }


def _write(path: Path, payload: dict) -> bool:
    """Persist, skipping a write that would only restamp a repeated failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_json_atomic_if_changed(path, payload, ("error.at",))


def record_pages(
    project: str,
    *,
    repo: str,
    pages: Sequence[IssuesResult],
    complete: bool,
) -> bool:
    """Fold one poll's pages into the mirror. Returns ``True`` iff it wrote."""
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

        payload = _document(
            repo=repo,
            fetched_at=fetched_at,
            since=since,
            rate=_rate_document(pages),
            error=None,
            issues=issues,
            issues_enabled=enabled,
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
