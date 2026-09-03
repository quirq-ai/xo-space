"""Centralised scope→handle resolution for the BFF layer.

BFF routes never construct paths themselves. They call
``resolve_scope("xo-projects")`` (returns a ``Path``) or
``resolve_scope("secrets")`` (returns a ``SecretsScope`` handle) and
delegate to service-layer helpers that own the actual filesystem
access.

This is principle P3 from docs/bff-endpoints-design.md: one place to
look when you need to know which on-disk location a frontend "noun"
maps to.

Visualizer scopes are read-only handles over ``<project>/.xo/`` (per
project), ``~/xo-projects/.xo/`` (workspace tier), and the watcher's
machine-local activity snapshots under ``~/.quirq/watcher/``.
They delegate all JSON reads to
``services/cowork_agent/visualizer/reader.py`` — the only module that
opens visualizer state files. See docs/watcher-design.md §6.0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from services.cowork_agent import project_layout
from services.cowork_agent.registry import agent_env
from services.cowork_agent.visualizer import reader as visualizer_reader
from services.cowork_agent.visualizer import state as watcher_state


class ScopeNotFound(Exception):
    """Raised when a caller asks for a scope name we don't recognise."""


class SecretsScope:
    """Handle exposing the secret store via agent_env helpers only.

    The BFF route only ever sees this object — never a raw Path — so it
    cannot accidentally read or write the underlying .env file
    directly. Future migrations (e.g. moving secrets out of .env into a
    real secret store) only need to swap this class's implementation.
    """

    def load(self) -> list[dict]:
        """Return current entries as [{key, value}, ...]."""
        return agent_env.load_env_entries()

    def save(self, items: list[dict]) -> None:
        """Bulk-replace the entire store."""
        agent_env.save_env_entries(items)

    def upsert(self, key: str, value: str) -> None:
        """Insert or update a single key (preserves comments/ordering)."""
        agent_env.upsert_env_entry(key, value)

    def delete(self, key: str) -> bool:
        """Remove a single key. Returns True if it was present."""
        entries = agent_env.load_env_entries()
        before = len(entries)
        kept = [e for e in entries if e.get("key") != key]
        if len(kept) == before:
            return False
        agent_env.save_env_entries(kept)
        return True


# ── Visualizer scopes (read-only) ──────────────────────────────────────────────


class _XoReader:
    """Shared read helpers for both visualizer scopes.

    Concrete subclasses set ``_xo_root`` to the root of the ``.xo/``
    directory they read from (per-project or workspace-tier) and
    ``_activity_path`` to the machine-local presence snapshot.
    """

    _xo_root: Path
    _activity_path: Path

    # Closed set of portable files the BFF endpoints read. Activity is
    # also a closed path, but lives outside _xo_root in _activity_path.
    # P4 / P6 — the route layer can't ask for an arbitrary path.
    _STATS:           str = "stats.json"
    _TODOS:           str = "todos.json"
    _TIMELINE:        str = "timeline.jsonl"
    _SESSIONSLIST:    str = "sessions/sessionslist.json"
    _SESSIONS_AUG:    str = "sessions/sessions-augment.json"

    def read_stats(self) -> Optional[dict]:
        return visualizer_reader.read_json(self._xo_root / self._STATS)

    def read_todos(self) -> Optional[dict]:
        return visualizer_reader.read_json(self._xo_root / self._TODOS)

    def read_activity(self) -> Optional[dict]:
        return visualizer_reader.read_json(self._activity_path)

    def read_timeline(
        self,
        *,
        limit: int,
        before: Optional[str] = None,
        types: Optional[frozenset[str]] = None,
    ) -> list[dict]:
        return visualizer_reader.read_jsonl_tail_reverse(
            self._xo_root / self._TIMELINE,
            limit=limit,
            before_ts=before,
            types=types,
        )

    def read_sessionslist(self) -> dict[str, dict]:
        """Merged sessionslist (adapter rows + watcher augment rows).

        Returns the flat ``{<composite_key>: <merged_row>}`` map the
        BFF endpoints serve. Empty dict if the adapter has never
        written a row for this scope.
        """
        base = visualizer_reader.read_json(self._xo_root / self._SESSIONSLIST)
        aug = visualizer_reader.read_json(self._xo_root / self._SESSIONS_AUG)
        return visualizer_reader.merge_sessionslist(base, aug)

    def read_one_session(self, identifier: str) -> Optional[tuple[str, dict]]:
        """Resolve a session by composite key, ``nativeSessionId``, or
        inner ``sessionId`` UUID.

        Mirrors the multi-id lookup pattern in
        ``services/cowork_agent/sessions_io.py:107``. The composite
        key is the row's true identity (the outer dict key in
        ``sessionslist.json``); ``nativeSessionId`` and inner
        ``sessionId`` are alternate handles the existing usage code
        uses.

        Returns ``(composite_key, merged_row)`` so callers don't lose
        track of which outer key matched. ``None`` if no match.
        """
        merged = self.read_sessionslist()
        if not merged:
            return None
        # 1. Exact composite-key match.
        if identifier in merged:
            return identifier, merged[identifier]
        # 2. Inner sessionId or nativeSessionId match.
        for key, row in merged.items():
            if row.get("nativeSessionId") == identifier:
                return key, row
            if row.get("sessionId") == identifier:
                return key, row
        return None


class VisualizerScope(_XoReader):
    """Read-only handle over ``<project>/.xo/`` for one project.

    ``project_id`` is resolved through
    ``project_layout.resolve_project_dirname`` before any FS lookup —
    it maps the id onto the directory that actually holds the project
    (folder name and id are the same thing) and, failing that, falls
    back to ``normalize_agent_id``. Either way the result is a safe
    leaf name — a traversal attempt like ``"../etc"`` collapses — so
    the path is always inside ``xo_projects_root()`` and we don't need
    a second clamp here (`bff-overview.md` §"Security properties").

    Exposes a small CRUD surface over ``.xo/todos.json`` for the
    agent-facing ``POST/PATCH/DELETE /todos`` endpoints. The CRUD
    helpers share :func:`visualizer.flock.locked` with the watcher's
    todos sink so the two writers never tear each other's edits.
    """

    def __init__(self, project_id: str) -> None:
        self.project_id = project_layout.resolve_project_dirname(project_id)
        self._xo_root = project_layout.xo_dir(self.project_id)
        self._activity_path = watcher_state.project_activity_path(self.project_id)

    def project_exists(self) -> bool:
        """Whether the project directory exists on disk.

        Used by routes to distinguish 404 ``project_not_found`` from
        empty-state ``200 {…: zeros}``.
        """
        return project_layout.project_dir_exists(self.project_id)

    # ── Todos CRUD (delegates to visualizer.todos_store) ──────────────

    def _todos_path(self):
        # Path is hidden behind this handle so route files don't need
        # to import pathlib (P2 grep stays clean).
        from services.cowork_agent.visualizer import todos_store  # noqa: F401
        return self._xo_root / "todos.json"

    def create_todo(self, **kwargs) -> dict:
        from services.cowork_agent.visualizer import todos_store
        return todos_store.create_todo(self._todos_path(), **kwargs)

    def get_todo(self, todo_id: str):
        from services.cowork_agent.visualizer import todos_store
        return todos_store.get_todo(self._todos_path(), todo_id)

    def update_todo(self, todo_id: str, **kwargs) -> dict:
        from services.cowork_agent.visualizer import todos_store
        return todos_store.update_todo(self._todos_path(), todo_id, **kwargs)

    def delete_todo(self, todo_id: str) -> bool:
        from services.cowork_agent.visualizer import todos_store
        return todos_store.delete_todo(self._todos_path(), todo_id)


class WorkspaceVisualizerScope(_XoReader):
    """Read-only handle over ``~/xo-projects/.xo/`` (workspace tier).

    Aggregate-of-all-projects view. The workspace ``.xo/`` is
    materialised by the watcher's workspace tier (see
    docs/watcher-design.md §3.2). Until the watcher's first tick,
    every read returns ``None`` / empty and the routes fall through
    to zero/empty payloads.
    """

    def __init__(self) -> None:
        self._xo_root = project_layout.workspace_xo_dir()
        self._activity_path = watcher_state.workspace_activity_path()

    def read_workspace(self) -> Optional[dict]:
        """``~/xo-projects/.xo/workspace.json`` — discovered project list."""
        return visualizer_reader.read_json(self._xo_root / "workspace.json")


# ── Resolver ──────────────────────────────────────────────────────────────────


ScopeHandle = Union[Path, SecretsScope, VisualizerScope, WorkspaceVisualizerScope]


def resolve_scope(name: str, *args) -> ScopeHandle:
    """Resolve a scope name to its handle.

    Returns a ``Path`` for filesystem scopes, or a domain-specific
    handle (``SecretsScope``, ``VisualizerScope``,
    ``WorkspaceVisualizerScope``).

    Variadic ``args`` carry scope-specific positional inputs:
    ``"xo-projects-visualizer"`` takes a ``project_id``; the others
    take none.
    """
    if name == "xo-projects":
        return project_layout.xo_projects_root()
    if name == "secrets":
        return SecretsScope()
    if name == "xo-projects-visualizer":
        if len(args) != 1 or not isinstance(args[0], str):
            raise ScopeNotFound(
                "xo-projects-visualizer requires a project_id string argument"
            )
        return VisualizerScope(args[0])
    if name == "xo-workspace-visualizer":
        return WorkspaceVisualizerScope()
    raise ScopeNotFound(f"Unknown scope: {name!r}")
