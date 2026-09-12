"""Who and where we are: the Space id (``XO_SPACE_ID``, on Coder and off) and the
workspace name and owner Coder reports when it runs the pod."""

from __future__ import annotations

import os
from typing import Optional

#: How many trailing characters of the workspace UUID go into the Space id.
_ID_SUFFIX_LEN = 6


def _env(name: str) -> Optional[str]:
    return (os.getenv(name, "") or "").strip() or None


def workspace_id() -> Optional[str]:
    """``XO_SPACE_ID``: the id the swarm knows this Space by. ``None`` when unset."""
    return _env("XO_SPACE_ID")


def workspace_name() -> Optional[str]:
    """``CODER_WORKSPACE_NAME`` — e.g. ``collabse``."""
    return _env("CODER_WORKSPACE_NAME")


def owner_name() -> Optional[str]:
    """``CODER_WORKSPACE_OWNER_NAME`` — e.g. ``ankitdwivedi``."""
    return _env("CODER_WORKSPACE_OWNER_NAME")


def space_id() -> Optional[str]:
    """``<owner>:<workspace>_<last 6 of XO_SPACE_ID>`` when Coder supplies the owner
    and workspace name, the bare ``XO_SPACE_ID`` otherwise, ``None`` when unset."""
    wid = workspace_id()
    if not wid:
        return None
    owner, name = owner_name(), workspace_name()
    if not owner or not name:
        # Partial environment. The raw id is wrong-but-honest; a composite with
        # an empty half would read as a real id and would not be one.
        return wid
    return f"{owner}:{name}_{wid[-_ID_SUFFIX_LEN:]}"


def resolve_user_id() -> str:
    """The owning user, in precedence order."""
    try:
        from routers.auth.auth import get_auth_state

        authed = get_auth_state().get("user_id")
        if authed:
            return str(authed)
    except Exception:
        pass
    return owner_name() or "local"


def is_placeholder_user_id(value: object) -> bool:
    """Whether a stored ``owner_user_id`` is the ``"local"`` placeholder."""
    return value is None or (isinstance(value, str) and value.strip() in ("", "local"))
