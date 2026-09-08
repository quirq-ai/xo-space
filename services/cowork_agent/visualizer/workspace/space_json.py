"""``<XO root>/.xo/space.json`` — the Space record (syncplan §5.3).

**This path used to hold the derived d3 graph.** The graph moved to
``~/.quirq/workspace/graph.json`` (T14, see ``views.graph_path``) and
``GET /xo/space.json`` still serves it, so nothing in the browser
changed. What the move bought is this file: a durable, low-churn record
of *what this Space is*, in a path with exactly one writer.

The graph could never hold identity. It is rebuilt by two writers that
share no lock — ``views.apply`` from the watcher tick and ``views.build``
from a request thread — so any field written beside it is destroyed on
the next rebuild.

``space_id`` is **captured, never minted.** ``CODER_WORKSPACE_ID`` is
externally assigned, stable across container rebuilds, and already read
by ``services/usage_sync.py`` for every outbound usage record: the
identity exists, so inventing a second one would only create a second
thing to reconcile. Off Coder there is no such id and the field is
``null`` — deliberately, per syncplan O1. **Never add a fallback that
mints one**; a locally minted id would diverge the moment the same Space
is opened on a machine that does have the environment.

Ownership. This sink owns every key here except ``label``, which is
seeded once from ``CODER_WORKSPACE_NAME`` and is then the user's — so
the write goes through :func:`write_json_owned`, which carries ``label``
(and anything a later writer adds) forward untouched.

Cost. The record is near-static, so it is rebuilt at most every
``XO_SPACE_REFRESH_S`` (default 60 s) and written only when it changes.
``last_used_at`` would otherwise churn the file once a second; it is
refreshed at most every ``XO_SPACE_AGENT_TOUCH_S`` (default 1 h), which
is the resolution the field is actually read at.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import services.cowork_agent.adapters as _adapters_pkg
from services.cowork_agent import coder_identity
from services.cowork_agent.local_state import quirq_state_dir
from services.cowork_agent.project_layout import workspace_xo_dir, xo_projects_root
from services.cowork_agent.registry.agent_registry import all_agents, get_active_agent
from services.cowork_agent.visualizer.atomic_write import (
    CorruptDocumentError,
    write_json_atomic,
    write_json_owned,
)
from services.cowork_agent.visualizer.reader import read_json

logger = logging.getLogger(__name__)

FILENAME = "space.json"
SCHEMA = 2

#: Every key this sink replaces on each write. ``label`` is deliberately
#: absent: it is user-editable, so it is seeded once (below) and then
#: carried forward by the merge like any other foreign key.
OWNS = frozenset(
    {
        "$schema",
        "schema",
        "space_id",
        "coder_workspace_id",
        "owner_user_id",
        "created_at",
        "updated_at",
        "roots",
        "agents",
    }
)

_REFRESH_DEFAULT_S = 60.0
_AGENT_TOUCH_DEFAULT_S = 3600.0
_ISO = "%Y-%m-%dT%H:%M:%SZ"

_last_build = 0.0  # monotonic; a clock jump must not pin the record stale
_legacy_replaced_logged = False

_ADAPTERS_DIR = Path(_adapters_pkg.__file__).resolve().parent


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime(_ISO)


def _env_seconds(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    try:
        return max(0.0, float(raw)) if raw.strip() else default
    except ValueError:
        return default


def refresh_seconds() -> float:
    return _env_seconds("XO_SPACE_REFRESH_S", _REFRESH_DEFAULT_S)


def agent_touch_seconds() -> float:
    return _env_seconds("XO_SPACE_AGENT_TOUCH_S", _AGENT_TOUCH_DEFAULT_S)


def path() -> Path:
    return workspace_xo_dir() / FILENAME


def space_id() -> Optional[str]:
    """The Space id — ``<owner>:<workspace>_<last6>``, ``None`` off Coder.

    e.g. ``ankitdwivedi:collabse_07c611``. Minted from Coder's own
    environment rather than captured verbatim (2026-09-08, reversing
    syncplan O1); :func:`coder_identity.space_id` carries the reasoning and
    the graded degradation. The raw ``CODER_WORKSPACE_ID`` is persisted
    beside it as ``coder_workspace_id``, so nothing has to parse the
    composite to recover the assigned id.
    """
    return coder_identity.space_id()


def _label() -> Optional[str]:
    return (os.getenv("CODER_WORKSPACE_NAME", "") or "").strip() or None


def _resolve_user_id() -> str:
    """Auth state, else the Coder workspace owner, else ``"local"``.

    Was three duplicated copies (here, ``sinks/project_json``,
    ``sinks/activity``) kept apart so a sink never imported another sink's
    privates. :mod:`services.cowork_agent.coder_identity` is not a sink, so
    it can hold the answer once and all three can agree on it.
    """
    return coder_identity.resolve_user_id()


def _capabilities(agent: str) -> list[str]:
    """Which capability modules ``adapters/<agent>/`` provides.

    The same on-disk discovery ``adapters/loader.list_capability_providers``
    does, read the other way round: one agent, every capability. Core code
    still never names an agent — the names come off the filesystem.
    """
    try:
        folder = _ADAPTERS_DIR / agent
        if not folder.is_dir():
            return []
        return sorted(
            entry.stem
            for entry in folder.glob("*.py")
            if entry.is_file()
            and not entry.stem.startswith("_")
            and entry.stem.isidentifier()
        )
    except Exception:
        return []


def _too_old(stamp: Any, *, now: datetime, max_age_s: float) -> bool:
    """Whether an ISO stamp is missing, unparseable, or past its age."""
    if not isinstance(stamp, str) or not stamp:
        return True
    try:
        seen = datetime.strptime(stamp, _ISO).replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    if seen > now + timedelta(seconds=1):
        return True  # a stamp from the future is not a stamp we can age
    return (now - seen).total_seconds() >= max_age_s


def _agents(current: dict, *, now: datetime) -> list[dict]:
    """The attached-backend roster, carrying its own history forward.

    ``first_seen_at`` is set once, when an agent first appears in the
    record. ``last_used_at`` moves only for the active agent, and then
    only when the stored stamp is older than the touch interval — a
    per-tick stamp would rewrite this file every second forever.
    """
    previous = {
        row.get("name"): row
        for row in (current.get("agents") or [])
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    try:
        active = get_active_agent().name
    except Exception:
        active = None
    try:
        manifests = sorted(all_agents(), key=lambda item: item.name)
    except Exception:
        logger.exception("space record: agent manifests unavailable")
        manifests = []

    stamp = _iso(now)
    touch = agent_touch_seconds()
    rows: list[dict] = []
    for manifest in manifests:
        prior = previous.get(manifest.name) or {}
        is_active = manifest.name == active
        first_seen = prior.get("first_seen_at")
        if not isinstance(first_seen, str) or not first_seen:
            first_seen = stamp
        last_used = prior.get("last_used_at")
        if is_active and _too_old(last_used, now=now, max_age_s=touch):
            last_used = stamp
        elif not isinstance(last_used, str) or not last_used:
            last_used = None
        rows.append(
            {
                "name": manifest.name,
                "active": is_active,
                "binary_available": shutil.which(manifest.binary) is not None,
                "capabilities": _capabilities(manifest.name),
                "first_seen_at": first_seen,
                "last_used_at": last_used,
            }
        )
    return rows


def build(current: Optional[dict] = None, *, now: Optional[datetime] = None) -> dict:
    """Assemble the record. ``current`` is the merge base.

    ``label`` is emitted in its schema position but is only *written* on
    the seeding pass; :func:`apply` drops it otherwise, so the key order
    of a freshly created file matches syncplan §5.3 without the sink ever
    claiming ownership of a field the user edits.
    """
    base = current if isinstance(current, dict) else {}
    moment = now or _now()
    stamp = _iso(moment)

    created_at = base.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        created_at = stamp

    # A stored owner is never overwritten — the same rule ``project.json``
    # follows, and for the same reason: a Space changing hands is far rarer
    # than a momentarily unreadable auth state, and reassigning ownership by
    # accident is not recoverable from the record itself.
    #
    # This used to test ``owner == "local"``, which was sufficient only while
    # "local" was the *only* fallback. Now that an unauthenticated Space
    # resolves to its Coder owner instead, that test would let the Coder name
    # overwrite a stored, authenticated id — so the condition is on what is
    # *stored*, not on what was resolved.
    owner = _resolve_user_id()
    stored_owner = base.get("owner_user_id")
    if not coder_identity.is_placeholder_user_id(stored_owner):
        owner = stored_owner

    return {
        "$schema": "xo/space.schema.json",
        "schema": SCHEMA,
        "space_id": space_id(),
        # The id Coder actually assigned, verbatim. ``space_id`` is now a
        # composite, and the whole point of O1's "captured, never minted"
        # was that the assigned id must not be lost — so it is kept, not
        # reconstructed by parsing the composite.
        "coder_workspace_id": coder_identity.workspace_id(),
        "label": _label(),
        "owner_user_id": owner,
        "created_at": created_at,
        "updated_at": stamp,
        "roots": {
            "projects_root": str(xo_projects_root()),
            "state_root": str(quirq_state_dir()),
        },
        "agents": _agents(base, now=moment),
    }


def apply(*, force: bool = False) -> bool:
    """Watcher entry point. Self-throttles; returns ``True`` when it wrote.

    Never raises: the workspace tier runs the sinks in one ``try``, so an
    exception here would cost every sink after it its tick.
    """
    global _last_build
    now = time.monotonic()
    if not force and (now - _last_build) < refresh_seconds():
        return False
    _last_build = now

    try:
        return _apply_body()
    except CorruptDocumentError:
        # Only reachable if the file was replaced between the read below and
        # the merge. Leave it alone rather than merging into nothing: a
        # user-edited `label` (and anything a later writer owns) would be
        # dropped silently, which is the bug the primitive refuses to commit.
        logger.warning(
            "space record became unreadable mid-write; leaving it for the "
            "next tick rather than merging into an unknown base"
        )
        return False
    except Exception:
        logger.exception("space record write failed")
        return False


def _apply_body() -> bool:
    global _legacy_replaced_logged
    target = path()
    try:
        current = read_json(target) if target.exists() else None
    except Exception:
        current = None

    # What sits at this path on an existing install is the *old graph* — a
    # derived document that now lives in ~/.quirq/workspace/graph.json — or
    # a scaffold placeholder, or an unparseable file. None of those has a
    # key worth preserving, so the record replaces them wholesale. Only a
    # document that already is a schema-2 record gets merged into.
    legacy = not isinstance(current, dict) or current.get("schema") != SCHEMA

    values = build(None if legacy else current)
    owns = set(OWNS)
    if legacy or "label" not in current:
        # Seed the user-editable label once; after that it is theirs.
        owns.add("label")
    else:
        values.pop("label", None)

    if legacy:
        if not _legacy_replaced_logged and isinstance(current, dict):
            _legacy_replaced_logged = True
            logger.info(
                "%s held a pre-T14 document (the derived graph now lives at "
                "%s); replacing it with the Space record",
                target,
                quirq_state_dir() / "workspace" / "graph.json",
            )
        write_json_atomic(target, values)
        return True
    return write_json_owned(
        target, owns=frozenset(owns), values=values, volatile=("updated_at",)
    )
