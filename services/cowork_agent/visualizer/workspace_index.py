"""Workspace discovery — every project the watcher should track.

Thin wrapper around ``services.cowork_agent.project_layout`` that
hides whether a project is scaffolded (has ``.xo/project.json``) or
bare. The watcher tracks every directory under ``xo_projects_root``
that *could* receive Claude/OpenClaw events — scaffolding state is
the watcher's own decision (the ``project_json`` sink fills identity
on first sight).

**Per-tick memo (docs/syncplan.md §10, T23).** ``list_project_ids()``
was called 8 times inside one watcher tick — six unconditional sinks
plus two from the active source — and each call re-walked the whole
root twice (``list_projects`` + ``list_unscaffolded_dirs``). The memo
here collapses that to one walk per tick.

It is deliberately **scoped**, not global: ``list_project_ids`` also
has a request-path caller
(``routers/cowork_agent/bff/workspace_visualizer.py:77``), and a
module-level cache with no invalidation would serve that route a stale
project list forever — a project created through the API would stay
invisible until restart. Only code that explicitly enters
:func:`project_index_scope` (the watcher, once per tick) gets the memo;
every other caller keeps today's always-fresh behaviour.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterable, Iterator

from services.cowork_agent.project_layout import (
    list_projects,
    list_unscaffolded_dirs,
    xo_projects_root,
)

# The memo lives in a ContextVar rather than a threading.local because
# the watcher runs its tick through ``asyncio.to_thread``, which copies
# the calling context into the worker thread: a var set inside the tick
# is visible to everything the tick calls (including the adapter source
# modules this package cannot import), is confined to that one context
# copy, and cannot leak into a FastAPI request handler — which runs in
# its own context and therefore always sees the default, ``None``.
_project_ids_memo: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "xo_project_ids_memo", default=None
)


@contextmanager
def project_index_scope() -> Iterator[None]:
    """Memoize :func:`list_project_ids` for the duration of the block.

    Correctness only has to hold for one watcher tick, so there are no
    invalidation semantics to get wrong: the scope is entered at the top
    of ``Watcher.tick()`` and left at the bottom, and the next tick
    starts from a fresh walk.
    """
    token = _project_ids_memo.set({})
    try:
        yield
    finally:
        _project_ids_memo.reset(token)


def _scan_project_ids() -> list[str]:
    out: set[str] = set()
    for entry in list_projects():
        name = entry.get("name")
        if name:
            out.add(name)
    for entry in list_unscaffolded_dirs():
        name = entry.get("name")
        if name:
            out.add(name)
    return sorted(out)


def list_project_ids() -> list[str]:
    """All project ids under ``~/xo-projects/`` (scaffolded + bare).

    Sorted alphabetically. Excludes the workspace-tier ``.xo/``
    directory itself (which starts with ``.`` and is already filtered
    by the underlying helpers).

    Inside a :func:`project_index_scope` the walk happens once and every
    later call in that scope returns a copy of the same list. Outside
    one — every request-path caller — the filesystem is walked, exactly
    as before.
    """
    memo = _project_ids_memo.get()
    if memo is None:
        return _scan_project_ids()
    cached = memo.get("ids")
    if cached is None:
        cached = _scan_project_ids()
        memo["ids"] = cached
    # Hand out a copy: callers own their list (``workspace_json`` puts it
    # straight into a payload) and must not be able to poison the memo.
    return list(cached)


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
