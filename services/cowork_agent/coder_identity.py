"""Who and where we are, as Coder reports it.

Three records carry an identity that until now resolved to the literal
``"local"`` whenever nobody had logged in: ``project.json:owner_user_id``,
``space.json:owner_user_id`` and the ``user_id`` on each ``activity.json``
presence row. Each sink resolved it privately, and each resolved it the same
wrong way — ``"local"`` is a *machine-scoped* answer written into records that
travel, so two Spaces both write it and the two values compare equal.

This module is the one place that answers the question. It is deliberately not
a sink: the sinks may import it without importing each other's privates, which
is the rule the three duplicated ``_resolve_user_id`` helpers existed to keep.

**Everything here is captured from the environment.** No network, no
subprocess, no state — every function is a few ``os.getenv`` calls, so it is
safe on a watcher tick and safe at import time. The environment is read on
*every* call rather than cached at import, so a container rebuild that changes
the workspace id is picked up without a restart.

Off Coder, every reader returns ``None`` and :func:`resolve_user_id` falls back
to ``"local"`` exactly as before.
"""

from __future__ import annotations

import os
from typing import Optional

#: How many trailing characters of the workspace UUID go into the Space id.
#: Six is enough to disambiguate a user's own workspaces without carrying the
#: whole UUID through every label and log line.
_ID_SUFFIX_LEN = 6


def _env(name: str) -> Optional[str]:
    return (os.getenv(name, "") or "").strip() or None


def workspace_id() -> Optional[str]:
    """``CODER_WORKSPACE_ID`` — the id Coder assigned. ``None`` off Coder."""
    return _env("CODER_WORKSPACE_ID")


def workspace_name() -> Optional[str]:
    """``CODER_WORKSPACE_NAME`` — e.g. ``collabse``."""
    return _env("CODER_WORKSPACE_NAME")


def owner_name() -> Optional[str]:
    """``CODER_WORKSPACE_OWNER_NAME`` — e.g. ``ankitdwivedi``.

    This is the workspace *owner's* Coder username. It is the only user
    identity Coder puts in the environment — there is no ``CODER_USER_ID`` —
    so it is what :func:`resolve_user_id` uses.
    """
    return _env("CODER_WORKSPACE_OWNER_NAME")


def space_id() -> Optional[str]:
    """``<owner>:<workspace>_<last 6 of workspace id>``, or ``None`` off Coder.

    e.g. ``ankitdwivedi:collabse_07c611``.

    **This is minted, and that is a reversal of syncplan O1**, which said the
    Space id is "captured, never minted… a locally generated fallback must
    never be committed, because it would diverge from the externally assigned
    id." The reversal is deliberate (2026-09-08): a bare UUID is unreadable in
    a UI, a log line or an assignment, and the composite carries the owner and
    the workspace name that make it recognisable to a human.

    The divergence O1 warned about is contained rather than ignored: every
    component is *derived from* Coder's own environment, the last six
    characters of the real id are embedded so the link back is never lost, and
    the raw id remains available from :func:`workspace_id` and is persisted
    verbatim alongside the composite in ``space.json``. Nothing has to
    reconstruct it by parsing.

    Degradation is graded, because a partial environment must not produce a
    half-formed id that looks real:

    * all three present  -> the composite
    * id but no owner/name -> the raw workspace id (today's behaviour)
    * no workspace id    -> ``None``
    """
    wid = workspace_id()
    if not wid:
        return None
    owner, name = owner_name(), workspace_name()
    if not owner or not name:
        # Partial environment. The raw id is wrong-but-honest; a composite
        # with an empty half would read as a real id and would not be one.
        return wid
    return f"{owner}:{name}_{wid[-_ID_SUFFIX_LEN:]}"


def resolve_user_id() -> str:
    """The owning user, in precedence order.

    1. the authenticated ``user_id`` from the auth state — a real, global
       identity, and the only one that survives leaving Coder;
    2. :func:`owner_name` — the Coder workspace owner;
    3. ``"local"``.

    The auth state wins because it is the stronger claim: it identifies a
    person to the wider system, whereas the Coder username identifies them
    only within this deployment. Coder sits *between* it and ``"local"``
    rather than replacing either — it turns the common unauthenticated case
    from a value that collides across every Space into one that does not.

    ``routers.auth`` is imported lazily because it triggers FastAPI app
    construction at import time in some test paths.
    """
    try:
        from routers.auth.auth import get_auth_state

        authed = get_auth_state().get("user_id")
        if authed:
            return str(authed)
    except Exception:
        pass
    return owner_name() or "local"


def is_placeholder_user_id(value: object) -> bool:
    """Whether a stored ``owner_user_id`` is the ``"local"`` placeholder.

    A stored owner is never overwritten — that would be silently taking
    someone else's project. ``"local"`` is the exception, and it is not
    really an owner at all: it is what every unauthenticated Space wrote
    before this module existed, so it identifies nobody and collides with
    every other Space that wrote it. Treating it as unset is what lets a
    real id land on a project that has one of these frozen into it.
    """
    return value is None or (isinstance(value, str) and value.strip() in ("", "local"))
