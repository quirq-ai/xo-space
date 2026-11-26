"""The GitHub reads a *person* triggers: fetch one issue or pull request, ask who we are."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .issues import (
    GH_BIN,
    GH_TIMEOUT_S,
    MERGEABLE_STATES,
    RateLimit,
    RepoRef,
    _coerce_ref,
    _STATE_REASONS,
    base_tip,
    _STATES,
    gh_available,
    label_names,
    person,
    run_graphql,
    run_rest,
    total_count,
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
    for who in (holder.get("nodes") if isinstance(holder, dict) else None) or []:
        assignee = person(who)
        if assignee is not None:
            assignees.append(assignee)
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

    answer = await run_graphql(argv, timeout_s=timeout_s)
    if not answer.ok:
        return _failure_issue(slug, number, answer.kind, answer.message, answer.rate)
    rate = answer.rate

    repository = (answer.data or {}).get("repository")
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
    answer = await run_rest([gh_bin, "api", "user"], timeout_s=timeout_s,
                            label="gh api user")
    if not answer.ok:
        return LoginResult(ok=False, error_kind=answer.kind, error=answer.message)
    payload = answer.data

    login = payload.get("login") if isinstance(payload, dict) else None
    if not isinstance(login, str) or not login:
        return LoginResult(
            ok=False, error_kind="bad_response",
            error="`gh api user` answered without a login.",
        )
    return LoginResult(ok=True, login=login)


# ── One issue or pull request, as a person opens it ──────────────────────────
# Fetched when someone opens the item, never polled: the body and the comments
# are the heaviest part of an issue and the least often looked at. Both
# queries walk a handful of connections once, so each costs 1 point.

#: The newest comments an item page shows. GitHub returns them oldest first
#: within the window, which is the order a page reads them in.
DETAIL_COMMENTS = 50

#: The checks a pull request page lists.
DETAIL_CHECKS = 50

_COMMENTS_SELECTION = f"""
      comments(last: {DETAIL_COMMENTS}) {{
        totalCount
        nodes {{ author {{ login avatarUrl }} body createdAt }}
      }}"""

ISSUE_DETAIL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number
      updatedAt
      body
      labels(first: 20) { nodes { name } }""" + _COMMENTS_SELECTION + """
    }
  }
}
"""

PULL_DETAIL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  rateLimit { limit cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number
      updatedAt
      body
      mergeable
      headRefOid
      baseRef { target { oid } }
      labels(first: 20) { nodes { name } }""" + _COMMENTS_SELECTION + f"""
      commits(last: 1) {{
        nodes {{
          commit {{
            statusCheckRollup {{
              state
              contexts(first: {DETAIL_CHECKS}) {{
                nodes {{
                  __typename
                  ... on CheckRun {{ name status conclusion }}
                  ... on StatusContext {{ context state }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""

#: A check run that has not finished reads as PENDING, the word the rollup
#: itself uses for "still running".
_UNFINISHED_RUN = frozenset({"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"})


@dataclass(frozen=True)
class DetailResult:
    """One item's body, comments and (for a pull request) checks, or why not."""

    ok: bool
    detail: dict[str, Any] | None = None
    rate: RateLimit = field(default_factory=RateLimit)
    error_kind: str | None = None
    error: str | None = None


def _comments(node: Any) -> list[dict[str, Any]]:
    holder = node.get("comments") if isinstance(node, dict) else None
    out: list[dict[str, Any]] = []
    for item in (holder.get("nodes") if isinstance(holder, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        comment: dict[str, Any] = {"author": person(item.get("author"))}
        if isinstance(item.get("body"), str):
            comment["body"] = item["body"]
        if isinstance(item.get("createdAt"), str):
            comment["created_at"] = item["createdAt"]
        out.append(comment)
    return out


def _checks(node: Any) -> list[dict[str, str]]:
    """Every check context on the head commit, as ``{name, state}``."""
    commits = node.get("commits") if isinstance(node, dict) else None
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    if not isinstance(nodes, list) or not nodes or not isinstance(nodes[-1], dict):
        return []
    commit = nodes[-1].get("commit")
    rollup = commit.get("statusCheckRollup") if isinstance(commit, dict) else None
    contexts = rollup.get("contexts") if isinstance(rollup, dict) else None
    out: list[dict[str, str]] = []
    for ctx in (contexts.get("nodes") if isinstance(contexts, dict) else None) or []:
        if not isinstance(ctx, dict):
            continue
        if ctx.get("__typename") == "CheckRun":
            name, conclusion = ctx.get("name"), ctx.get("conclusion")
            status = str(ctx.get("status") or "").upper()
            state = (str(conclusion).upper() if isinstance(conclusion, str) and conclusion
                     else "PENDING" if status in _UNFINISHED_RUN or not status else status)
        else:
            name, state = ctx.get("context"), str(ctx.get("state") or "").upper()
        if isinstance(name, str) and name and state:
            out.append({"name": name, "state": state})
    return out


async def fetch_detail(
    repo: RepoRef | str,
    number: int,
    *,
    pull: bool,
    timeout_s: float = GH_TIMEOUT_S,
    gh_bin: str = GH_BIN,
) -> DetailResult:
    """One issue's or pull request's body and comments, and a pull request's
    checks and merge facts. **Never raises.** One GraphQL point."""
    ref = repo if isinstance(repo, RepoRef) else _coerce_ref(str(repo or ""))
    if ref is None:
        return DetailResult(False, error_kind="bad_remote",
                            error=f"Not a GitHub owner/repo: {repo!r}.")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return DetailResult(False, error_kind="not_found",
                            error="An issue or pull request number is a positive integer.")
    if not gh_available(gh_bin):
        return DetailResult(False, error_kind="no_cli",
                            error="GitHub CLI (`gh`) is not installed on this machine.")
    argv = [
        gh_bin, "api", "graphql",
        "-f", f"query={PULL_DETAIL_QUERY if pull else ISSUE_DETAIL_QUERY}",
        "-f", f"owner={ref.owner}",
        "-f", f"name={ref.name}",
        "-F", f"number={number}",
    ]
    if not ref.is_github_com:
        argv[3:3] = ["--hostname", ref.host]

    answer = await run_graphql(argv, timeout_s=timeout_s)
    what = "pull request" if pull else "issue"
    if not answer.ok:
        if answer.kind == "unknown" and "could not resolve to" in answer.message.lower():
            # GitHub's answer for a number that is not this kind of item.
            return DetailResult(False, rate=answer.rate, error_kind="not_found",
                                error=f"{ref.slug} has no {what} #{number}.")
        return DetailResult(False, rate=answer.rate, error_kind=answer.kind, error=answer.message)
    repository = (answer.data or {}).get("repository")
    node = (repository or {}).get("pullRequest" if pull else "issue") if isinstance(repository, dict) else None
    if not isinstance(node, dict) or node.get("number") != number:
        return DetailResult(False, rate=answer.rate, error_kind="not_found",
                            error=f"{ref.slug} has no {what} #{number} this GitHub account can see.")

    detail: dict[str, Any] = {
        "number": number,
        "updated_at": node.get("updatedAt") if isinstance(node.get("updatedAt"), str) else None,
        "body": node.get("body") if isinstance(node.get("body"), str) else "",
        "labels": label_names(node.get("labels")) or [],
        "comments": _comments(node),
        "comment_count": total_count(node.get("comments")),
    }
    if pull:
        detail["checks"] = _checks(node)
        mergeable = str(node.get("mergeable") or "").upper()
        detail["mergeable"] = mergeable if mergeable in MERGEABLE_STATES else "UNKNOWN"
        detail["head_oid"] = node.get("headRefOid") if isinstance(node.get("headRefOid"), str) else None
        detail["base_oid"] = base_tip(node)
    return DetailResult(True, detail=detail, rate=answer.rate)
