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

:func:`list_project_pids` is the pid-keyed companion and shares that memo.
It is a second projection of the same walk, not a second walk.
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


def _scan_projects() -> dict[str, str | None]:
    """The one walk: directory name → ``project.json:pid``, every project.

    Both public projections come from this. :func:`list_projects` already
    parses every ``project.json``, so keeping the ``pid`` costs nothing over
    discarding it — but *walking twice* to get ids and pids separately would
    cost a second pass per tick, which is exactly the T23 regression the
    memo exists to prevent. Hence one scan, two views.

    ``None`` for a project whose identity has not been minted yet (a bare
    folder, or a template not yet filled). Callers decide what a pid-less
    project means to them; this does not invent one.
    """
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
    """Every project's durable pid, keyed by directory name.

    Deliberately **separate from** :func:`list_project_ids` rather than a
    change to it. The two answer different questions and the distinction is
    load-bearing: the directory name is the lookup key everywhere (see
    ``project_layout.list_projects``, which overrides a stale stored
    ``name`` for exactly that reason), while ``pid`` is the identity that
    survives a rename, a clone and a restore. Folding them together would
    give one function two meanings, and every caller would have to know
    which one it got.

    Returns the mapping, not a bare list of pids, because a caller that
    wants to key something by pid still has to *find* the project — and
    every path helper takes the directory name.

    Shares the same scan as :func:`list_project_ids`, so a watcher tick that
    asks for both still walks the root exactly once (T23).
    """
    # A copy, for the same reason ``list_project_ids`` hands one out: the
    # memo is shared for the whole tick and a caller must not poison it.
    return dict(_scanned())


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
