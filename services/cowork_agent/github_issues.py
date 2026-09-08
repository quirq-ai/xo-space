"""GitHub issues for an XO project, read through the `gh` CLI.

Router-facing facade in the same shape as project_sharing.service: typed
errors, no HTTP. The project is a folder under XO_PROJECTS_ROOT; its origin
remote names the GitHub repo. Only github.com origins are supported, since
`gh` is GitHub's own client.

Auth follows the auto-clone rule: when the XO Space GitHub connector holds a
token it is handed to `gh` as GH_TOKEN, so issues come from the account
connected in Setup. With no connector token `gh` falls back to its own login,
whatever the shell has. `gh` runs argv-only through utils.commands.run.

Two reads:
  list_issues(project_id, state, limit)  -> number / title / state only
  get_issue(project_id, number)          -> everything, comments included
"""
from __future__ import annotations

import json
import os

from services.cowork_agent.project_layout import project_dir, project_dir_exists
from services.cowork_agent.project_sharing.repo_identity import normalize_repo
from utils.commands import run

GH_TIMEOUT_SECONDS = 30.0
MAX_LIMIT = 100
VALID_STATES = ("open", "closed", "all")

_LIST_FIELDS = "number,title,state"
_VIEW_FIELDS = "number,title,state,body,author,labels,assignees,createdAt,updatedAt,closedAt,url,comments"


class IssuesError(Exception):
    status = 500
    code = "issues_error"
    message = "GitHub issues error."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message


class ProjectNotFound(IssuesError):
    status, code, message = 404, "project_not_found", "Project not found."


class NoGitOrigin(IssuesError):
    status, code, message = 404, "no_git_origin", "This project has no git origin."


class NotGitHub(IssuesError):
    status, code, message = 400, "not_github", "This project's origin is not on github.com."


class GhMissing(IssuesError):
    status, code, message = 503, "gh_missing", "The GitHub CLI (gh) is not installed on this workspace."


class GhUnauthenticated(IssuesError):
    status, code, message = 401, "gh_unauthenticated", "GitHub is not connected. Connect GitHub in Setup, or run `gh auth login` on the workspace."


class NotFound(IssuesError):
    status, code, message = 404, "not_found", "Issue or repository not found."


class Forbidden(IssuesError):
    status, code, message = 403, "forbidden", "The connected GitHub account cannot see this repository."


class GhTimeout(IssuesError):
    status, code, message = 504, "gh_timeout", "GitHub did not answer in time."


class GhFailed(IssuesError):
    status, code, message = 502, "gh_failed", "gh failed."


# ── project → repo ────────────────────────────────────────────────────────────

async def _origin_url(path) -> str | None:
    res = await run(["git", "-C", str(path), "config", "--get", "remote.origin.url"],
                    timeout=GH_TIMEOUT_SECONDS)
    out = res.output.strip()
    return out if res.ok and out else None


async def resolve_repo(project_id: str) -> str:
    """`owner/name` for the project's origin, or a typed error."""
    if not project_dir_exists(project_id):
        raise ProjectNotFound()
    identity = normalize_repo(await _origin_url(project_dir(project_id)))
    if identity is None:
        raise NoGitOrigin()
    host, _, owner_name = identity.partition("/")
    if host != "github.com" or "/" not in owner_name:
        raise NotGitHub()
    return owner_name


# ── gh ────────────────────────────────────────────────────────────────────────

def _connector_token() -> str | None:
    """The XO Space connector's GitHub token, or None. Lazy import: the
    connector package pulls httpx, which a unit test of this module must not need."""
    try:
        from services.cowork_agent.connectors.github import get_github_token
        return get_github_token() or None
    except Exception:  # noqa: BLE001 — connector unavailable == no token
        return None


def gh_env() -> dict[str, str]:
    env = dict(os.environ)
    token = _connector_token()
    if token:
        env["GH_TOKEN"] = token
    return env


_UNAUTH_MARKERS = ("gh auth login", "not logged in", "authentication", "http 401", "bad credentials")
_NOT_FOUND_MARKERS = ("http 404", "not found", "could not resolve to an issue", "could not resolve to a repository")
_FORBIDDEN_MARKERS = ("http 403", "forbidden", "resource not accessible", "permission")


def classify_failure(output: str) -> IssuesError:
    text = (output or "").lower()
    if any(m in text for m in _UNAUTH_MARKERS):
        return GhUnauthenticated()
    if any(m in text for m in _NOT_FOUND_MARKERS):
        return NotFound()
    if any(m in text for m in _FORBIDDEN_MARKERS):
        return Forbidden()
    last = [l for l in (output or "").splitlines() if l.strip()]
    return GhFailed(f"gh failed: {last[-1].strip()}" if last else "gh failed")


def parse_json(output: str):
    """gh's JSON, tolerating a warning line before it (stdout and stderr are merged)."""
    text = output or ""
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    if not starts:
        raise GhFailed("gh returned no JSON")
    try:
        return json.loads(text[min(starts):])
    except json.JSONDecodeError as exc:
        raise GhFailed(f"gh returned unreadable JSON: {exc}") from exc


async def _gh(argv: list[str]):
    res = await run(["gh", *argv], env=gh_env(), timeout=GH_TIMEOUT_SECONDS)
    if res.binary_missing:
        raise GhMissing()
    if res.timed_out:
        raise GhTimeout()
    if not res.ok:
        raise classify_failure(res.output)
    return parse_json(res.output)


# ── shaping ───────────────────────────────────────────────────────────────────

def _login(obj) -> str | None:
    return (obj or {}).get("login") if isinstance(obj, dict) else None


def _shape_issue(raw: dict) -> dict:
    return {
        "number": raw.get("number"),
        "title": raw.get("title") or "",
        "state": (raw.get("state") or "").lower(),
        "body": raw.get("body") or "",
        "author": _login(raw.get("author")),
        "labels": [l.get("name") for l in raw.get("labels") or [] if isinstance(l, dict) and l.get("name")],
        "assignees": [a.get("login") for a in raw.get("assignees") or [] if isinstance(a, dict) and a.get("login")],
        "created_at": raw.get("createdAt"),
        "updated_at": raw.get("updatedAt"),
        "closed_at": raw.get("closedAt"),
        "url": raw.get("url"),
        "comments": [
            {"author": _login(c.get("author")), "body": c.get("body") or "", "created_at": c.get("createdAt")}
            for c in raw.get("comments") or [] if isinstance(c, dict)
        ],
    }


# ── public ────────────────────────────────────────────────────────────────────

async def list_issues(project_id: str, state: str = "open", limit: int = 30) -> dict:
    if state not in VALID_STATES:
        raise ValueError(f"state must be one of {VALID_STATES}")
    repo = await resolve_repo(project_id)
    n = max(1, min(int(limit), MAX_LIMIT))
    rows = await _gh(["issue", "list", "--repo", repo, "--state", state,
                      "--limit", str(n), "--json", _LIST_FIELDS])
    issues = [{"number": r.get("number"), "title": r.get("title") or "",
               "state": (r.get("state") or "").lower()}
              for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
    return {"project_id": project_id, "repo": repo, "state": state, "issues": issues}


async def get_issue(project_id: str, number: int) -> dict:
    repo = await resolve_repo(project_id)
    raw = await _gh(["issue", "view", str(int(number)), "--repo", repo, "--json", _VIEW_FIELDS])
    if not isinstance(raw, dict):
        raise GhFailed("gh returned an unexpected shape")
    return {"project_id": project_id, "repo": repo, "issue": _shape_issue(raw)}
