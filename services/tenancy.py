"""tenancy.py — which workspace am I?

One Coder workspace = one pod = one tenant. This module answers only the half of that
identity the pod itself knows: its own ``CODER_WORKSPACE_ID``.

**It does not compose the tenant key.** The principal — ``<account>__ws__<workspace>`` —
is composed by xo-swarm-api (``auth/principal.py``) from the credential this backend
authenticates with plus the workspace id it supplies, and reaches this process through
``connectors.composio.state.principal()``. There is exactly one composer, in one repo,
because the principal is stored inside Composio against every connected account and two
implementations drifting apart would orphan all of them.

Pod-local state (``data/``, ``.env``, ``~/.xo-cowork/``, the ``.xo/`` trees) needs no
scoping: one pod is one disk, so it is already isolated. Scope only what crosses the
boundary — and that scoping now happens on the other side of it.

**Fail closed.** There is no default and no ``"unknown"`` bucket: a workspace id that
falls back to a shared constant would merge every misconfigured pod into one tenant. A
deployment that gets no ``CODER_WORKSPACE_ID`` therefore has no tenant, and the Composio
routes 401 until one is injected.
"""

from __future__ import annotations

import os

# Injected by the Coder pod. The sole source of workspace identity.
WORKSPACE_ENV = "CODER_WORKSPACE_ID"


class WorkspaceIdentityUnavailable(RuntimeError):
    """CODER_WORKSPACE_ID is unset or empty, so there is no tenant to scope to."""


def workspace_id() -> str:
    """This pod's workspace id.

    Read at call time, not import time, so an operator (or a verification run) can change
    the environment without reimporting.

    Raises:
        WorkspaceIdentityUnavailable: when the variable is unusable. Callers must surface
            this, never substitute a default.
    """
    value = (os.getenv(WORKSPACE_ENV) or "").strip()
    if value:
        return value
    raise WorkspaceIdentityUnavailable(f"{WORKSPACE_ENV} is not set")
