"""The GitHub reads a *person* triggers: fetch one issue, ask who we are."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .issues import (
    GH_BIN,
    GH_TIMEOUT_S,
    RateLimit,
    RepoRef,
    _classify_graphql_errors,
    _classify_rest_error,
    _classify_stderr,
    _coerce_ref,
    _parse_rate,
    _run_gh,
    _STATE_REASONS,
    _STATES,
    gh_available,
)

#: The pinned single-issue query.
ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      number
      title
      url
      state
      stateReason
      updatedAt
      labels(first: 20) { nodes { name } }
      assignees(first: 5) { nodes { login avatarUrl } }
    }
  }
}
"""


@dataclass(frozen=True)
class IssueResult:
    """One issue as GitHub last described it, or why it could not be read."""

    ok: bool
    repo: str | None
    number: int | None
    issue: dict[str, Any] | None = None
    rate: RateLimit = field(default_factory=RateLimit)
    error_kind: str | None = None
    error: str | None = None


def _failure_issue(repo: str | None, number: int | None, kind: str, message: str,
                   rate: RateLimit | None = None) -> IssueResult:
    return IssueResult(
        ok=False, repo=repo, number=number, rate=rate or RateLimit(),
        error_kind=kind, error=message,
    )


def _issue_row(node: Any, *, repo: str) -> dict[str, Any] | None:
    """One GraphQL issue node → the shape adoption and the mirror both use."""
    if not isinstance(node, dict):
        return None
    node_id = node.get("id")
    number = node.get("number")
    if not isinstance(node_id, str) or not node_id:
        return None
    if not isinstance(number, int) or isinstance(number, bool):
        return None
    state = _STATES.get(str(node.get("state") or "").upper())
    if state is None:
        return None
    labels: list[str] = []
    holder = node.get("labels")
    for label in (holder.get("nodes") if isinstance(holder, dict) else None) or []:
        name = label.get("name") if isinstance(label, dict) else None
        if isinstance(name, str) and name and name not in labels:
            labels.append(name)
    assignees: list[dict[str, Any]] = []
    holder = node.get("assignees")
    for person in (holder.get("nodes") if isinstance(holder, dict) else None) or []:
        if not isinstance(person, dict):
            continue
        login = person.get("login")
        if not isinstance(login, str) or not login:
            continue
        avatar = person.get("avatarUrl")
        assignees.append({
            "login": login,
            "avatar_url": avatar if isinstance(avatar, str) else None,
        })
    url = node.get("url")
    updated = node.get("updatedAt")
    return {
        "node_id": node_id,
        "number": number,
        "repo": repo,
        "title": str(node.get("title") or ""),
        "state": state,
        "state_reason": _STATE_REASONS.get(str(node.get("stateReason") or "").upper()),
        "assignees": assignees,
        "labels": labels,
        "url": url if isinstance(url, str) else "",
        "updated_at": updated if isinstance(updated, str) else "",
    }


async def fetch_issue(
    repo: RepoRef | str,
    number: int,
    *,
    timeout_s: float = GH_TIMEOUT_S,
    gh_bin: str = GH_BIN,
) -> IssueResult:
    """One issue, with its labels. **Never raises.** One GraphQL point."""
    ref = repo if isinstance(repo, RepoRef) else _coerce_ref(str(repo or ""))
    if ref is None:
        return _failure_issue(
            None, number, "bad_remote",
            f"Not a GitHub owner/repo: {repo!r}." if repo else "No repository given.",
        )
    slug = ref.slug
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return _failure_issue(
            slug, None, "bad_remote", "An issue number must be a positive integer.",
        )
    if not gh_available(gh_bin):
        return _failure_issue(
            slug, number, "no_cli",
            "GitHub CLI (`gh`) is not installed on this machine. Install it "
            "from https://cli.github.com/ — adopting an issue needs it.",
        )

    argv = [
        gh_bin, "api", "graphql",
        "-f", f"query={ISSUE_QUERY}",
        "-f", f"owner={ref.owner}",
        "-f", f"name={ref.name}",
        "-F", f"number={number}",
    ]
    if not ref.is_github_com:
        argv[3:3] = ["--hostname", ref.host]

    result = await _run_gh(argv, timeout_s)
    if result.binary_missing:
        return _failure_issue(slug, number, "no_cli",
                              "GitHub CLI (`gh`) could not be executed.")
    if result.timed_out:
        return _failure_issue(
            slug, number, "timeout",
            f"`gh api graphql` did not answer within {timeout_s:g}s.",
        )
    if result.exception:
        return _failure_issue(slug, number, "unknown",
                              f"Could not run `gh`: {result.exception}")
    returncode, stdout, stderr = result.returncode, result.stdout, result.stderr

    payload: Any = None
    if stdout.strip():
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            payload = None
    rate = _parse_rate(payload)

    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            classified = _classify_graphql_errors(errors)
            if classified:
                return _failure_issue(slug, number, classified[0], classified[1], rate)
        if "data" not in payload:
            classified = _classify_rest_error(payload)
            if classified:
                return _failure_issue(slug, number, classified[0], classified[1], rate)

    if returncode != 0:
        kind, message = _classify_stderr(stderr, returncode)
        return _failure_issue(slug, number, kind, message, rate)
    if not isinstance(payload, dict):
        return _failure_issue(slug, number, "bad_response",
                              "`gh api graphql` returned no JSON body.", rate)

    data = payload.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    node = repository.get("issue") if isinstance(repository, dict) else None
    row = _issue_row(node, repo=slug)
    if row is None:
        return _failure_issue(
            slug, number, "not_found",
            f"{slug}#{number} is not an issue this GitHub account can see. "
            f"A pull request number resolves here as 'not found' too — they "
            f"are a different connection entirely.",
            rate,
        )
    return IssueResult(ok=True, repo=slug, number=number, issue=row, rate=rate)


@dataclass(frozen=True)
class LoginResult:
    """Who ``gh`` is authenticated as on this machine, or why it is unknown."""

    ok: bool
    login: str | None = None
    error_kind: str | None = None
    error: str | None = None


async def authenticated_login(
    *, timeout_s: float = GH_TIMEOUT_S, gh_bin: str = GH_BIN,
) -> LoginResult:
    """This Space's own GitHub login, via ``gh api user``. **Never raises.**"""
    if not gh_available(gh_bin):
        return LoginResult(
            ok=False, error_kind="no_cli",
            error="GitHub CLI (`gh`) is not installed on this machine.",
        )
    result = await _run_gh([gh_bin, "api", "user"], timeout_s)
    if result.binary_missing:
        return LoginResult(ok=False, error_kind="no_cli",
                           error="GitHub CLI (`gh`) could not be executed.")
    if result.timed_out:
        return LoginResult(
            ok=False, error_kind="timeout",
            error=f"`gh api user` did not answer within {timeout_s:g}s.",
        )
    if result.exception:
        return LoginResult(ok=False, error_kind="unknown",
                           error=f"Could not run `gh`: {result.exception}")
    returncode, stdout, stderr = result.returncode, result.stdout, result.stderr
    payload: Any = None
    if stdout.strip():
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            payload = None
    if returncode != 0:
        if isinstance(payload, dict):
            classified = _classify_rest_error(payload)
            if classified:
                return LoginResult(ok=False, error_kind=classified[0],
                                   error=classified[1])
        kind, message = _classify_stderr(stderr, returncode)
        return LoginResult(ok=False, error_kind=kind, error=message)
    login = payload.get("login") if isinstance(payload, dict) else None
    if not isinstance(login, str) or not login:
        return LoginResult(
            ok=False, error_kind="bad_response",
            error="`gh api user` answered without a login.",
        )
    return LoginResult(ok=True, login=login)
