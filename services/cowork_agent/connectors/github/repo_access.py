"""
GitHub connector — which repositories this workspace may touch.

The `gh auth login` device flow authorizes GitHub CLI's OAuth app, and an
OAuth token is all-or-nothing: its ``repo`` scope covers every repository the
account can reach, and GitHub offers no per-repository choice for it. So the
choice is made here instead, and enforced by checks on our side:

  * ``mode == "all"``       every repository is allowed (the default, and what
                            every pre-existing connection keeps).
  * ``mode == "selected"``  only the listed ``owner/repo`` slugs are allowed.
                            An empty list allows nothing — that is the state
                            between "signed in" and "picked repositories".

This is an application-level allowlist, NOT a GitHub-side restriction: the
stored token itself can still reach every repository, and so can anything that
uses it without going through :func:`is_repo_allowed`. A hard boundary needs a
fine-grained PAT (the other connect method) instead.

The selection lives beside the token, in the same ``github`` entry of
token.json, so it is dropped with the token and never outlives the account it
was chosen for.
"""

import logging
import re
from typing import Any, Literal

import httpx

from ..token_store import get_entry, set_entry
from .common import GITHUB_API, get_github_token

log = logging.getLogger(__name__)

RepoAccessMode = Literal["all", "selected"]
MODES: tuple[str, ...] = ("all", "selected")

_ENTRY_KEY = "repo_access"

# `owner/repo`, in the characters GitHub accepts for each half.
_SLUG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")

# Bounds on one selection and on one listing, so neither a request body nor an
# account with thousands of repositories can grow without limit.
MAX_SELECTED_REPOS = 500
_PER_PAGE = 100
_MAX_PAGES = 10


class RepoAccessError(Exception):
    """A caller-actionable failure: bad input, or no connection to act on."""


def normalize_slug(value: Any) -> str | None:
    """``owner/repo`` in canonical lowercase, or ``None`` if it is not a slug."""
    if not isinstance(value, str):
        return None
    slug = value.strip().removesuffix(".git")
    if not _SLUG_RE.match(slug):
        return None
    return slug.lower()


def _clean(raw: Any) -> dict[str, Any]:
    """The stored selection, tolerating a missing or hand-edited value."""
    if not isinstance(raw, dict) or raw.get("mode") != "selected":
        return {"mode": "all", "repos": []}
    repos = raw.get("repos") if isinstance(raw.get("repos"), list) else []
    slugs = [s for s in (normalize_slug(r) for r in repos) if s]
    return {"mode": "selected", "repos": sorted(set(slugs))}


def get_repo_access() -> dict[str, Any]:
    """The current selection: ``{"mode": "all"|"selected", "repos": [...]}``."""
    entry = get_entry("github")
    return _clean(entry.get(_ENTRY_KEY) if entry else None)


def set_repo_access(mode: str, repos: list[str] | None = None) -> dict[str, Any]:
    """Store the selection on the connected account. Returns what was stored.

    Raises RepoAccessError when nothing is connected or the input is invalid.
    """
    if mode not in MODES:
        raise RepoAccessError('Repository access must be "all" or "selected".')

    slugs: list[str] = []
    if mode == "selected":
        for raw in repos or []:
            slug = normalize_slug(raw)
            if slug is None:
                raise RepoAccessError(f"Not a GitHub owner/repo: {raw!r}.")
            slugs.append(slug)
        slugs = sorted(set(slugs))
        if len(slugs) > MAX_SELECTED_REPOS:
            raise RepoAccessError(
                f"At most {MAX_SELECTED_REPOS} repositories can be selected."
            )

    entry = get_entry("github")
    if not entry or not entry.get("access_token"):
        raise RepoAccessError("GitHub is not connected. Sign in first.")

    access = {"mode": mode, "repos": slugs}
    set_entry("github", {**entry, _ENTRY_KEY: access})
    log.info("GitHub repository access set to %s (%d selected)", mode, len(slugs))
    _notify_issue_poller()
    return access


def is_repo_allowed(slug: str | None) -> bool:
    """Whether this workspace may touch ``owner/repo``. Never raises.

    The check every repo-scoped GitHub call makes before it spends a request.
    With nothing stored (or mode "all") everything is allowed, so connections
    that predate this feature behave exactly as before.
    """
    try:
        access = get_repo_access()
    except Exception:
        # An unreadable store cannot prove the repo was selected: fail closed.
        log.warning("Could not read GitHub repository access", exc_info=True)
        return False
    if access["mode"] == "all":
        return True
    normalized = normalize_slug(slug)
    return normalized is not None and normalized in access["repos"]


NOT_SELECTED_MESSAGE = (
    "This repository is not among the repositories selected for this "
    "workspace. Add it under Connectors → GitHub → Repository access."
)


def _notify_issue_poller() -> None:
    """A repo that was just allowed must not wait out a stale backoff."""
    try:
        from services.cowork_agent import github_poller

        github_poller.note_auth_change()
    except Exception:
        log.debug("could not notify the GitHub issue poller", exc_info=True)


async def list_accessible_repos() -> list[dict[str, Any]]:
    """Every repository the connected account can reach, most recent first.

    Feeds the picker. Raises RepoAccessError when not connected or when GitHub
    cannot be reached; capped at ``_MAX_PAGES * _PER_PAGE`` repositories.
    """
    token = get_github_token()
    if not token:
        raise RepoAccessError("GitHub is not connected. Sign in first.")

    repos: list[dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for page in range(1, _MAX_PAGES + 1):
                resp = await client.get(
                    f"{GITHUB_API}/user/repos",
                    params={"per_page": _PER_PAGE, "page": page, "sort": "pushed"},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                )
                if resp.status_code in (401, 403):
                    raise RepoAccessError("GitHub rejected the stored token. Sign in again.")
                if resp.status_code != 200:
                    raise RepoAccessError(f"GitHub returned HTTP {resp.status_code}.")
                batch = resp.json()
                if not isinstance(batch, list):
                    raise RepoAccessError("GitHub returned an unexpected repository list.")
                for repo in batch:
                    full_name = repo.get("full_name") if isinstance(repo, dict) else None
                    if isinstance(full_name, str) and full_name:
                        repos.append({
                            "full_name": full_name,
                            "private": bool(repo.get("private")),
                        })
                if len(batch) < _PER_PAGE:
                    break
    except httpx.HTTPError as exc:
        raise RepoAccessError(f"Could not reach GitHub: {type(exc).__name__}.") from exc
    return repos
