"""
GitHub connector — PAT (Personal Access Token) acquisition.

No environment variables. No OAuth app. The user generates a PAT on GitHub,
pastes it into the UI, and this module validates it and signs the GitHub CLI
in with it — the non-interactive equivalent of:

    gh auth login --with-token < token.txt
    gh auth setup-git

so the token lives in gh's credential store and git borrows it through
`gh auth git-credential`, exactly as after the device flow.

This is one of two acquisition methods; the other is the `gh auth login` device
flow in ``cli_auth.py``. Everything the two share — storage, validation,
status — lives in ``common.py``.
"""

import logging
import re
from typing import Any

from .common import (
    configure_git_identity,
    connection_payload,
    gh_available,
    login_gh_with_token,
    note_github_connected,
    sign_gh_out_of_other_accounts,
    validate_token,
)

log = logging.getLogger(__name__)

AUTH_METHOD = "pat"

# Prefixes GitHub uses for tokens a user can paste: classic PAT (ghp_),
# fine-grained PAT (github_pat_), OAuth token (gho_).
_TOKEN_PREFIXES = ("ghp_", "github_pat_", "gho_")
_MIN_TOKEN_LENGTH = 30

# `gh auth login` refuses a token that carries OAuth scopes (a classic PAT,
# including the old 40-hex kind, or an OAuth token) unless it has `repo` and
# `read:org`; `write:org` and `admin:org` include `read:org`. Fine-grained PATs
# have no scopes and are not checked. This is gh's own rule, applied before gh
# sees the token so the refusal can name every missing scope at once.
_SCOPED_TOKEN_RE = re.compile(r"(?:ghp_|gho_)\S+|[0-9a-f]{40}")
_REQUIRED_SCOPES = (
    ("repo", {"repo"}),
    ("read:org", {"read:org", "write:org", "admin:org"}),
)
TOKENS_PAGE = "https://github.com/settings/tokens"
# The failure code the UI turns into its own message; it never renders ours.
MISSING_SCOPES = "missing_scopes"


def looks_like_token(token: str) -> bool:
    """Cheap client-side sanity check before spending a round-trip on GitHub."""
    return token.startswith(_TOKEN_PREFIXES) or len(token) >= _MIN_TOKEN_LENGTH


def missing_required_scopes(token: str, scopes: str) -> list[str]:
    """The scopes gh needs that this token lacks, given its X-OAuth-Scopes
    header. Empty for a fine-grained PAT, which has no scopes to check."""
    if not _SCOPED_TOKEN_RE.fullmatch(token):
        return []
    granted = {scope.strip() for scope in scopes.split(",")}
    return [name for name, satisfied_by in _REQUIRED_SCOPES if granted.isdisjoint(satisfied_by)]


def _missing_scopes_error(missing: list[str]) -> str:
    names = " and ".join(f"`{name}`" for name in missing)
    noun = "scope" if len(missing) == 1 else "scopes"
    return (f"This token is missing the {names} {noun} GitHub CLI needs. Edit it at "
            f"{TOKENS_PAGE}, tick {names}, and save the token again.")


async def connect(token: str) -> dict[str, Any]:
    """
    Validate a pasted PAT and, if it is good, hand it to gh's credential store.

    Returns:
        {"ok": True,  "payload": <connection body>}                on success
        {"ok": False, "status": "needs_auth"|"failed", "error": ...} otherwise,
        plus "code": "missing_scopes" and "missing_scopes": [...] when a
        classic token lacks the scopes gh needs
    """
    if not gh_available():
        return {
            "ok": False,
            "status": "failed",
            "error": "GitHub CLI (`gh`) is not installed on the server, and it holds "
                     "the token. Install it from https://cli.github.com/.",
        }

    result = await validate_token(token)

    if not result.get("valid"):
        return {
            "ok": False,
            "status": result["status"],
            "error": result.get("error", "Validation failed."),
        }

    missing = missing_required_scopes(token, result.get("scopes", ""))
    if missing:
        return {
            "ok": False,
            "status": "needs_auth",
            "code": MISSING_SCOPES,
            "missing_scopes": missing,
            "error": _missing_scopes_error(missing),
        }

    failure = await login_gh_with_token(token)
    if failure:
        return {"ok": False, **failure}

    # Only once gh has accepted the new token: a rejected replacement leaves
    # the current connection as it was.
    await sign_gh_out_of_other_accounts()
    note_github_connected()
    await configure_git_identity(result, setup_credential_helper=True)
    log.info("GitHub connected as @%s (via PAT)", result.get("username"))
    return {"ok": True, "payload": connection_payload(result, AUTH_METHOD)}
