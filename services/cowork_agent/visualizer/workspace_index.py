"""Workspace discovery — every project the watcher should track."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterable, Iterator

from services.cowork_agent.project_layout import (
    list_projects,
    list_unscaffolded_dirs,
    xo_projects_root,
)

# The memo lives in a ContextVar rather than a threading.local because the
# watcher runs its tick through ``asyncio.to_thread``, which copies the calling
# context into the worker thread: a var set inside the tick is visible to
# everything the tick calls (including the adapter source modules this package
# cannot import), is confined to that one context copy, and cannot leak into a
# FastAPI request handler — which runs in its own context and therefore always
# sees the default, ``None``.
_project_ids_memo: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "xo_project_ids_memo", default=None
)


@contextmanager
def project_index_scope() -> Iterator[None]:
    """Memoize :func:`list_project_ids` for the duration of the block."""
    token = _project_ids_memo.set({})
    try:
        yield
    finally:
        _project_ids_memo.reset(token)


def _scan_projects() -> dict[str, str | None]:
    """The one walk: directory name → ``project.json:pid``, every project."""
    out: dict[str, str | None] = {}
    for entry in list_projects():
        name = entry.get("name")
        if not name:
            continue
        raw = entry.get("pid")
        out[name] = raw.strip() if isinstance(raw, str) and raw.strip() else None
    for entry in list_unscaffolded_dirs():
        name = entry.get("name")
        if name and name not in out:
            out[name] = None
    return out


def _scanned() -> dict[str, str | None]:
    """The scan, memoized for the tick when there is a scope to memoize in."""
    memo = _project_ids_memo.get()
    if memo is None:
        return _scan_projects()
    cached = memo.get("scan")
    if cached is None:
        cached = _scan_projects()
        memo["scan"] = cached
    return cached


def list_project_pids() -> dict[str, str | None]:
    """Every project's durable pid, keyed by directory name."""
    # A copy, for the same reason ``list_project_ids`` hands one out: the memo
    # is shared for the whole tick and a caller must not poison it.
    return dict(_scanned())


def list_project_ids() -> list[str]:
    """All project ids under ``~/xo-projects/`` (scaffolded + bare)."""
    # Hand out a fresh list: callers own it (``workspace_json`` puts it
    # straight into a payload) and must not be able to poison the memo.
    return sorted(_scanned())


def iter_project_xo_dirs() -> Iterable[tuple[str, "Path"]]:
    """Yield ``(project_id, <project>/.xo/)`` for every project.

    The watcher's per-project sink loop uses this. Imported lazily to
    keep ``Path`` out of the public type surface where it isn't
    needed.
    """
    from pathlib import Path
    from services.cowork_agent.project_layout import xo_dir

    for pid in list_project_ids():
        yield pid, xo_dir(pid)


def workspace_root() -> "Path":
    """Re-exported for sinks that need the root without importing
    ``project_layout`` directly."""
    return xo_projects_root()
