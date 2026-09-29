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
from typing import Any

from .common import (
    configure_git_identity,
    connection_payload,
    gh_available,
    login_gh_with_token,
    note_github_connected,
    validate_token,
)

log = logging.getLogger(__name__)

AUTH_METHOD = "pat"

# Prefixes GitHub uses for tokens a user can paste: classic PAT (ghp_),
# fine-grained PAT (github_pat_), OAuth token (gho_).
_TOKEN_PREFIXES = ("ghp_", "github_pat_", "gho_")
_MIN_TOKEN_LENGTH = 30


def looks_like_token(token: str) -> bool:
    """Cheap client-side sanity check before spending a round-trip on GitHub."""
    return token.startswith(_TOKEN_PREFIXES) or len(token) >= _MIN_TOKEN_LENGTH


async def connect(token: str) -> dict[str, Any]:
    """
    Validate a pasted PAT and, if it is good, hand it to gh's credential store.

    Returns:
        {"ok": True,  "payload": <connection body>}                on success
        {"ok": False, "status": "needs_auth"|"failed", "error": ...} otherwise
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

    failure = await login_gh_with_token(token)
    if failure:
        return {"ok": False, **failure}

    note_github_connected()
    await configure_git_identity(result, setup_credential_helper=True)
    log.info("GitHub connected as @%s (via PAT)", result.get("username"))
    return {"ok": True, "payload": connection_payload(result, AUTH_METHOD)}
