"""The GitHub reads a *person* triggers: fetch one issue, ask who we are.

``docs/workitems-plan.md`` §7.2 (W7). :mod:`.github_issues` is the poll — one
repo's page of issues, once a minute, on a budget. This module is its
interactive sibling: one issue, on demand, because a human clicked something.
They are separate for reasons that are not stylistic.

* **Different cadence, different cost rules.** The poll is pinned to one
  GraphQL point because it runs 60 times an hour per repo and the ceiling is
  the binding constraint (§6.2). This call happens when someone adopts an
  issue, which is rare, so it can afford the ``labels`` connection the poll
  cannot: adoption's whole job is to take the one-time snapshot §5.3 asks
  for, and *lazily at adoption* is exactly where the plan puts the label
  fetch. Measured against ``cjpais/Handy`` on 2026-09-08:
  :data:`ISSUE_QUERY` with ``labels(first:20)`` and ``assignees(first:5)``
  costs **1 point**, the same as the poll — the label cost the plan warns
  about is per *page of 100 issues*, not per issue.

* **Everything here is a read.** There was a ``set_assignees`` beside these
  two — a ``PATCH /repos/{owner}/{repo}/issues/{n}`` that wrote an
  ``assignees`` array, because D1 put coordination in GitHub. **That decision
  was reversed** (§13, amendment 33): assignment is now a local annotation in
  ``.xo/workitems.json`` for every workitem, adopted or not, and *nothing in
  this system writes to GitHub*. The function was deleted rather than left
  unused, so there is no half-live write path for a future caller to rewire
  by accident. GitHub remains the authority for an adopted issue's
  ``status``/``state_reason``/``body``, which this module and the poller
  read and never set.

**It never raises**, exactly like :mod:`.github_issues`, and for the same
reason: no ``gh``, no auth, no network, a deleted repo and a spent budget are
five *states* a caller has to render, not five exceptions. ``error_kind`` is
one of that module's :data:`~.github_issues.ERROR_KINDS`.

**Why it imports five private helpers from that module.** ``_run_gh``,
``_parse_rate``, ``_classify_graphql_errors``, ``_classify_rest_error`` and
``_classify_stderr`` are the contract for *how this system talks to gh*: a
process group that can be killed as a tree, a token injected into the child's
environment and nowhere else, and one table mapping gh's output onto the
closed error vocabulary the schema and the UI share. Re-implementing them
here would produce a second table that could disagree with the first — the
defect this codebase names outright when it refuses a second error mapping
for the claim routes. Borrowing them keeps one.

One thing that is *not* here: nothing in this module writes anything, to disk
or to GitHub. Adoption records go through ``workitems_store``, and the mirror
stays single-writer — the poller — which is why the label fetch happens here
rather than being folded into the poll (§5.2, amendment 2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .github_issues import (
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

#: The pinned single-issue query. Pinned for the same reason the poll's is:
#: cost is a property of the query shape, and a shape that is assembled at
#: the call site is not a shape anyone can measure.
#:
#: ``repository.issue(number:)`` resolves **issues only** — a pull request
#: number comes back ``null`` with a ``NOT_FOUND`` error rather than as an
#: issue. That is the same structural property §6.1 relies on, arriving from
#: the other direction: there is nothing to filter, because a PR cannot be
#: returned by an issue selector.
#:
#: ``labels(first: 20)`` is a literal, like the poll's ``assignees(first: 5)``
#: and for the same reason — a pinned query assembled from a constant is not
#: pinned. Twenty is well past what any issue carries in practice.
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
    """One GraphQL issue node → the shape adoption and the mirror both use.

    Deliberately the mirror's row shape (§5.2) plus ``labels``: the caller
    may fall back to a mirror row when this call fails, and two shapes for
    one thing would make that fallback a translation step nobody remembers to
    keep in sync.
    """
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
    """One issue, with its labels. **Never raises.** One GraphQL point.

    This is what "labels are fetched lazily at adoption" means (§5.3,
    amendment 1). It also means adoption does not depend on the poller having
    run: a project whose mirror is empty can still adopt an issue, which
    matters because the mirror is exactly what a brand-new project does not
    have yet.
    """
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

    returncode, stdout, stderr = await _run_gh(argv, timeout_s)
    if stderr == "__no_cli__":
        return _failure_issue(slug, number, "no_cli",
                              "GitHub CLI (`gh`) could not be executed.")
    if stderr == "__timeout__":
        return _failure_issue(
            slug, number, "timeout",
            f"`gh api graphql` did not answer within {timeout_s:g}s.",
        )
    if stderr.startswith("__spawn_failed__"):
        return _failure_issue(
            slug, number, "unknown",
            f"Could not run `gh`: {stderr[len('__spawn_failed__'):].strip()}",
        )

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
    """This Space's own GitHub login, via ``gh api user``. **Never raises.**

    §4 is the reason this is enough: *each Space only needs to know its own
    GitHub login*. GitHub does the routing, so there is no workspace → login
    mapping table anywhere in this design, and "assign to me" needs exactly
    one fact that this call answers.

    It goes through ``gh`` rather than only through the stored PAT because
    the two auth paths are different populations: a user who pasted a token
    has one in ``mcp-tokens.json``, while a user who ran the device-flow
    login has a ``gh`` session and may have no stored token at all.
    ``_subprocess_env`` injects the stored token when there is one, so this
    single call covers both. It spends one **core** REST point, not a
    GraphQL one — the poller's budget is untouched.
    """
    if not gh_available(gh_bin):
        return LoginResult(
            ok=False, error_kind="no_cli",
            error="GitHub CLI (`gh`) is not installed on this machine.",
        )
    returncode, stdout, stderr = await _run_gh([gh_bin, "api", "user"], timeout_s)
    if stderr == "__no_cli__":
        return LoginResult(ok=False, error_kind="no_cli",
                           error="GitHub CLI (`gh`) could not be executed.")
    if stderr == "__timeout__":
        return LoginResult(
            ok=False, error_kind="timeout",
            error=f"`gh api user` did not answer within {timeout_s:g}s.",
        )
    if stderr.startswith("__spawn_failed__"):
        return LoginResult(
            ok=False, error_kind="unknown",
            error=f"Could not run `gh`: {stderr[len('__spawn_failed__'):].strip()}",
        )
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
