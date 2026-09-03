"""
tenancy.py — workspace identity, the tenant half of every principal.

One Coder workspace = one pod = one tenant. The XO/Clerk ``user_id`` returned by
``/get-user-id`` is an **account** id: every workspace an account owns resolves
to the same value, so it is not a tenant key on its own. Anything keyed *outside*
this pod — Composio users above all — must carry the workspace half too.

Pod-local state (``data/``, ``.env``, ``~/.xo-cowork/``, the ``.xo/`` trees) needs
no scoping: one pod is one disk, so it is already isolated. Scope only what
crosses the boundary.

**Fail closed.** There is no default and no ``"unknown"`` bucket: falling back to
the bare account id would silently merge every workspace of an account into one
Composio tenant, which is precisely the bug this module prevents. A deployment
that gets no ``CODER_WORKSPACE_ID`` therefore has no tenant, and the Composio
routes 401 until one is injected.

This is the only module that reads workspace identity from the environment.
"""

from __future__ import annotations

import os

# Injected by the Coder pod. The sole source of workspace identity.
WORKSPACE_ENV = "CODER_WORKSPACE_ID"

# Alphanumeric + underscore: safe in URLs, JSON bodies and query params, and
# injective against Clerk ids, which are "user_" + base58 and cannot contain it.
SEPARATOR = "__ws__"


class WorkspaceIdentityUnavailable(RuntimeError):
    """CODER_WORKSPACE_ID is unset or empty, so there is no tenant to scope to."""


def workspace_id() -> str:
    """The workspace half of the tenant key.

    Read at call time, not import time, so an operator (or a verification run)
    can change the environment without reimporting.

    Raises:
        WorkspaceIdentityUnavailable: when the variable is unusable. Callers must
            surface this, never substitute a default.
    """
    value = (os.getenv(WORKSPACE_ENV) or "").strip()
    if value:
        return value
    raise WorkspaceIdentityUnavailable(f"{WORKSPACE_ENV} is not set")


def scoped_principal(account_id: str | None) -> str:
    """Compose an account id and the workspace id into a tenant principal.

    Idempotent: a value that already carries ``SEPARATOR`` is returned unchanged,
    so double application is harmless if another caller is ever added.

    Raises:
        ValueError: on a blank account id (mirrors
            ``composio.service._require_user_id``).
        WorkspaceIdentityUnavailable: from :func:`workspace_id`.
    """
    account = (account_id or "").strip()
    if not account:
        raise ValueError(
            f"tenancy.scoped_principal: a real account id is required (got {account_id!r})."
        )
    if SEPARATOR in account:
        return account
    return f"{account}{SEPARATOR}{workspace_id()}"


def is_scoped(principal: str | None) -> bool:
    """True iff ``principal`` carries a workspace half.

    Used to reject rows written before workspace scoping existed: a bare account
    id read back from disk would still address the account-wide bucket.
    """
    return bool(principal) and SEPARATOR in principal
