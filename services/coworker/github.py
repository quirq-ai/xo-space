"""A GitHub project's pull requests, and one issue or pull request as a person
opens it, for the coworker's Issues & PRs page.

Read-only towards GitHub (workitems plan, amendment 33). The pull request list
is the core poll's mirror, never a call per request: the poll reads it in the
same one-point query as the issues. An opened item's body, comments and checks
are fetched once and kept next to the mirror (``github/items/``); opening it
again costs nothing until the poll sees the item change, and when GitHub can't
be reached the last copy is served, marked ``stale``.

Which files a conflicting pull request touches is not something GitHub says.
The project's own clone works it out with ``git merge-tree`` when it already
has both commits, without fetching and without writing into the repository
(new objects go to a throwaway folder).
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from services.cowork_agent import github_poller, scopes
from services.cowork_agent.connectors.github import issue_actions
from services.cowork_agent.connectors.github.issues import ERROR_ANSWERS
from services.cowork_agent.visualizer import github_mirror
from services.errors import ServiceError
from services.git_safe import git, git_bytes
from services.project_files.address import project_root
from services.storage.atomic_write import write_json_atomic_if_changed
from utils.safe_read import read_text_guarded

logger = logging.getLogger(__name__)

#: The cold read's bounds, the same as the issue list's: one page, and no
#: longer than a person will wait for a panel to open.
COLD_FETCH_PAGES = 1
COLD_FETCH_TIMEOUT_S = 8.0

#: The most conflicting paths a page is given.
MAX_CONFLICTS = 200

#: Where opened items are kept, below the mirror's own folder.
ITEMS_DIRNAME = "items"


class GithubRefused(ServiceError):
    """A GitHub page that can't be answered, with the status it deserves."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project(project_id: str) -> tuple[scopes.VisualizerScope, str]:
    """The project's handle and its ``owner/name``, or a refusal."""
    scope = scopes.resolve_scope("xo-projects-visualizer", project_id)
    if not scope.project_exists():
        raise GithubRefused("project_not_found", "Project not found.", status=404)
    repo = scope.github_repo()
    if not repo:
        raise GithubRefused(
            "not_a_github_project",
            "This project has no github.com remote in project.json, so it has no issues or pull requests here.",
        )
    return scope, repo


def _refusal(kind: Optional[str], message: Optional[str], *, not_found: str) -> GithubRefused:
    status, code = ERROR_ANSWERS.get(kind or "", (502, "github_unavailable"))
    if kind == "not_found":
        code = not_found
    return GithubRefused(code, message or f"GitHub call failed ({kind}).", status=status)


def _drop_none(value: Any) -> Any:
    """``value`` without its ``None`` entries, recursively. The page checks
    types strictly and reads a missing key as "not known", never a null."""
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


# ── The pull request list ────────────────────────────────────────────────────

#: The keys of a mirror row the page is given (``head_oid``/``base_oid`` stay
#: inside Space: they only decide whether a merge state is still current).
_PULL_WIRE_KEYS = (
    "number", "title", "url", "state", "updated_at", "is_draft", "author", "head", "base", "checks",
    "review_decision", "mergeable", "additions", "deletions", "changed_files", "comment_count",
)


async def pulls(project_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """The mirror's pull requests, newest-updated first."""
    scope, repo = _project(project_id)
    mirror = scope.read_github_mirror()
    if refresh or github_mirror.mirror_pulls(mirror) is None:
        # A mirror written before pull requests were polled, or never written:
        # one page now rather than an empty list until the next tick.
        outcome = await github_poller.poll_project_now(
            project_id, max_pages_override=COLD_FETCH_PAGES, timeout=COLD_FETCH_TIMEOUT_S,
        )
        if outcome.polled:
            mirror = scope.read_github_mirror()
    rows = github_mirror.mirror_pulls(mirror) or {}
    ordered = sorted(rows.values(), key=lambda r: (str(r.get("updated_at") or ""), r["number"]), reverse=True)
    doc = mirror if isinstance(mirror, dict) else {}
    error = doc.get("error") if isinstance(doc.get("error"), dict) else None
    return _drop_none({
        "project_id": project_id,
        "repo": repo,
        "fetched_at": doc.get("fetched_at") if isinstance(doc.get("fetched_at"), str) else None,
        "open_total": doc.get("open_pulls_total") if isinstance(doc.get("open_pulls_total"), int) else None,
        "error": {k: error.get(k) for k in ("kind", "message", "at") if isinstance(error.get(k), str)}
        if error else None,
        "pulls": [{key: row.get(key) for key in _PULL_WIRE_KEYS} for row in ordered],
    })


# ── One item ─────────────────────────────────────────────────────────────────


def _item_path(project_id: str, number: int, *, pull: bool) -> Optional[Path]:
    path = github_mirror.mirror_path(project_id)
    if path is None:
        return None
    return path.parent / ITEMS_DIRNAME / f"{'pull' if pull else 'issue'}-{number}.json"


def _read_item(path: Optional[Path], repo: str) -> Optional[dict]:
    if path is None:
        return None
    try:
        doc = json.loads(read_text_guarded(path))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError):
        logger.warning("github item copy %s is unreadable; it will be fetched again", path)
        return None
    if not isinstance(doc, dict) or doc.get("repo") != repo or not isinstance(doc.get("detail"), dict):
        return None
    return doc


def _write_item(path: Optional[Path], doc: dict) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic_if_changed(path, doc, ())
    except OSError:
        # The copy only saves a later call; the answer in hand is still good.
        logger.warning("could not keep a copy of %s", path, exc_info=True)


def _mirror_updated_at(mirror: Optional[dict], number: int, *, pull: bool) -> Optional[str]:
    """When the poll last saw item ``number`` change, if it has seen it."""
    rows = github_mirror.mirror_pulls(mirror) if pull else (mirror or {}).get("issues")
    for row in (rows or {}).values() if isinstance(rows, dict) else ():
        if isinstance(row, dict) and row.get("number") == number:
            stamp = row.get("updated_at")
            return stamp if isinstance(stamp, str) and stamp else None
    return None


def _budget_refusal() -> Optional[str]:
    """Why an interactive call must wait, when the poller has stood down."""
    snapshot = github_poller.budget_snapshot()
    if snapshot.get("paused"):
        return f"GitHub calls are paused: {snapshot.get('pause_reason') or 'the poller backed off'}."
    remaining = snapshot.get("remaining")
    if isinstance(remaining, int) and remaining <= 0:
        return f"The GraphQL budget is spent; it refills at {snapshot.get('reset_at') or 'the next reset'}."
    return None


def _wire(doc: dict, *, pull: bool, stale: bool) -> dict[str, Any]:
    detail = doc["detail"]
    out: dict[str, Any] = {
        "number": detail.get("number"),
        "updated_at": detail.get("updated_at"),
        "body": detail.get("body") if isinstance(detail.get("body"), str) else "",
        "labels": detail.get("labels") if isinstance(detail.get("labels"), list) else [],
        "comments": detail.get("comments") if isinstance(detail.get("comments"), list) else [],
        "comment_count": detail.get("comment_count"),
        "fetched_at": doc.get("fetched_at"),
        "stale": stale,
    }
    if pull:
        out["checks"] = detail.get("checks") if isinstance(detail.get("checks"), list) else []
        out["mergeable"] = detail.get("mergeable")
        # Left out, not ``[]``, when the files can't be named: ``[]`` would say
        # "no conflicts" about a pull request GitHub says conflicts.
        out["conflicts"] = detail.get("conflicts")
    return _drop_none(out)


async def item(project_id: str, number: int, *, pull: bool, refresh: bool = False) -> dict[str, Any]:
    """One issue's or pull request's body and comments, and a pull request's
    checks and conflicting files."""
    scope, repo = _project(project_id)
    not_found = "pull_not_found" if pull else "issue_not_found"
    if number < 1:
        raise GithubRefused(not_found, "An issue or pull request number is a positive integer.", status=404)
    path = _item_path(project_id, number, pull=pull)
    kept = _read_item(path, repo)
    seen = _mirror_updated_at(scope.read_github_mirror(), number, pull=pull)
    if kept and not refresh and seen and kept["detail"].get("updated_at") == seen:
        return _wire(kept, pull=pull, stale=False)

    why_not = _budget_refusal()
    if why_not:
        if kept:
            return _wire(kept, pull=pull, stale=True)
        raise GithubRefused("github_rate_limited", why_not + " It clears on its own.", status=503)

    result = await issue_actions.fetch_detail(repo, number, pull=pull)
    github_poller.observe_rate(result.rate)
    if not result.ok or result.detail is None:
        if result.error_kind != "not_found" and kept:
            logger.info("github: serving the kept copy of %s#%s (%s)", repo, number, result.error_kind)
            return _wire(kept, pull=pull, stale=True)
        raise _refusal(result.error_kind, result.error, not_found=not_found)

    detail = dict(result.detail)
    if pull:
        mergeable = detail.get("mergeable")
        # ``[]`` only when GitHub says it merges cleanly; while GitHub is still
        # working it out (UNKNOWN), nothing is claimed either way.
        detail["conflicts"] = (
            await conflicting_files(project_id, detail.get("base_oid"), detail.get("head_oid"))
            if mergeable == "CONFLICTING" else [] if mergeable == "MERGEABLE" else None
        )
    doc = {"repo": repo, "fetched_at": _utc_now(), "detail": detail}
    _write_item(path, doc)
    return _wire(doc, pull=pull, stale=False)


# ── Conflicting files, from the local clone ──────────────────────────────────


async def conflicting_files(project_id: str, base_oid: Any, head_oid: Any) -> Optional[list[str]]:
    """The paths a merge of ``head_oid`` into ``base_oid`` conflicts on, or
    ``None`` when the clone can't say (a commit it hasn't fetched, a git
    older than 2.38, no clone). Never fetches, never raises, and never
    writes into the repository: the merge's objects go to a folder of its
    own that is deleted after."""
    if not (isinstance(base_oid, str) and isinstance(head_oid, str) and base_oid and head_oid):
        return None
    try:
        _, root = project_root(project_id)
    except ServiceError:
        return None
    try:
        for oid in (base_oid, head_oid):
            present = await git(root, "cat-file", "-e", f"{oid}^{{commit}}", log_output=False)
            if not present.ok:
                return None
        objects = await git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", log_output=False)
        if not objects.ok or not objects.output.strip():
            return None
        common = Path(objects.output.strip().splitlines()[-1]) / "objects"
        with tempfile.TemporaryDirectory(prefix="xo-merge-tree-") as scratch:
            env = {"GIT_OBJECT_DIRECTORY": scratch, "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common)}
            code, out = await git_bytes(root, "merge-tree", "--write-tree", "--name-only", "--no-messages",
                                        "-z", base_oid, head_oid, env=env)
    except Exception:  # pragma: no cover - a local read must never cost the page its answer
        logger.warning("github: could not work out the conflicting files in %s", project_id, exc_info=True)
        return None
    if code == 0:
        # The clone merges it cleanly where GitHub says it conflicts (a newer
        # base on GitHub than here): it can't name the files.
        return None
    if code != 1:
        return None
    names = [part.decode("utf-8", "replace") for part in out.split(b"\0")[1:] if part]
    unique: list[str] = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return unique[:MAX_CONFLICTS]
