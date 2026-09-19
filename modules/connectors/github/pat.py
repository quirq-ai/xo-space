"""
GitHub connector — PAT (Personal Access Token) acquisition.

No environment variables. No OAuth app. The user generates a fine-grained PAT
on GitHub, pastes it into the UI, and this module validates it and hands it to
the shared core for storage.

This is one of two acquisition methods; the other is the `gh auth login` device
flow in ``cli_auth.py``. Everything the two share — storage, validation,
status — lives in ``common.py``.
"""

import logging
from typing import Any

from .common import (
    configure_git_identity,
    connection_payload,
    save_github_token,
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
    Validate a pasted PAT and, if it is good, store it.

    Returns:
        {"ok": True,  "payload": <connection body>}                on success
        {"ok": False, "status": "needs_auth"|"failed", "error": ...} otherwise
    """
    result = await validate_token(token)

    if not result.get("valid"):
        return {
            "ok": False,
            "status": result["status"],
            "error": result.get("error", "Validation failed."),
        }

    save_github_token(token, auth_method=AUTH_METHOD)
    # Identity only — a pasted PAT leaves no `gh` session for git to borrow.
    await configure_git_identity(result, setup_credential_helper=False)
    log.info("GitHub connected as @%s (via PAT)", result.get("username"))
    return {"ok": True, "payload": connection_payload(result, AUTH_METHOD)}
