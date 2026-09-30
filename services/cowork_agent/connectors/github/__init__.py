"""
GitHub connector.

Two ways to sign the GitHub CLI in, one shared everything-else:

  * ``pat``      — the user pastes a personal access token
  * ``cli_auth`` — the `gh auth login` device flow
  * ``common``   — storage (gh's own), validation and status, shared by both

Callers that only need the connected identity should import from this package
(``from ...connectors.github import get_github_token``) and stay unaware of
which method established it.
"""

from . import cli_auth, pat
from .common import (
    GITHUB_API,
    AuthMethod,
    GitHubStatus,
    commit_email,
    configure_git_identity,
    connection_payload,
    disconnect_github_account,
    get_github_auth_method,
    get_github_token,
    get_status,
    validate_token,
)

__all__ = [
    "GITHUB_API",
    "AuthMethod",
    "GitHubStatus",
    "cli_auth",
    "commit_email",
    "configure_git_identity",
    "connection_payload",
    "disconnect_github_account",
    "get_github_auth_method",
    "get_github_token",
    "get_status",
    "pat",
    "validate_token",
]
