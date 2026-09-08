"""Centralised scope→handle resolution for the BFF layer.

BFF routes never construct paths themselves. They call
``resolve_scope("xo-projects")`` (returns a ``Path``) or
``resolve_scope("secrets")`` (returns a ``SecretsScope`` handle) and
delegate to service-layer helpers that own the actual filesystem
access.

This is principle P3 from docs/bff-endpoints-design.md: one place to
look when you need to know which on-disk location a frontend "noun"
maps to.

Visualizer scopes are read-only handles over **three** roots, not one
(docs/syncplan.md §9, T19 / Appendix A.2):

* the **synced** root — ``<project>/.xo/`` — ``todos.json`` and the four
  todo CRUD methods, plus the workspace registry;
* the **runtime** root — ``~/.quirq/projects/<key>/`` per project, and
  ``~/.quirq/workspace/`` for the workspace rollups (T20) — ``stats.json``,
  ``timeline.jsonl`` and the session index, which are machine-local
  derived state and never travel with a project;
* the watcher's machine-local activity snapshots under
  ``~/.quirq/watcher/``, which already lived there.

They delegate all JSON reads to
``services/cowork_agent/visualizer/reader.py`` — the only module that
opens visualizer state files. See docs/watcher-design.md §6.0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from services.cowork_agent import project_layout
from services.cowork_agent.engine import sessions_io as session_index
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

    Concrete subclasses set ``_xo_root`` to the **synced** root they read
    from, ``_runtime_root`` to the root holding the derived files T19
    moved out of the project tree, and ``_activity_path`` to the
    machine-local presence snapshot.

    ``_runtime_root`` may be ``None`` — a project whose folder is gone, or
    whose identity has not been minted yet, simply has no runtime home.
    Every runtime read below therefore answers empty rather than raising:
    a scope is constructed on the request path, and "no data yet" must
    never surface as a 500.
    """

    _xo_root: Path
    _runtime_root: Optional[Path]
    _activity_path: Path

    # Closed set of files the BFF endpoints read, each named against the
    # root that owns it. Activity is also a closed path, but lives outside
    # both roots in _activity_path.
    # P4 / P6 — the route layer can't ask for an arbitrary path.
    _TODOS:           str = "todos.json"          # synced
    _STATS:           str = "stats.json"          # runtime
    _TIMELINE:        str = "timeline.jsonl"      # runtime
    _SESSIONS_AUG:    str = "sessions/sessions-augment.json"   # runtime

    # ── Root-aware primitives ────────────────────────────────────────

    def _runtime_path(self, relative: str) -> Optional[Path]:
        """Resolve one runtime-tier file, or ``None`` when there is no
        runtime root to resolve it against."""
        if self._runtime_root is None:
            return None
        return self._runtime_root / relative

    def _read_runtime_json(self, relative: str) -> Optional[dict]:
        path = self._runtime_path(relative)
        return visualizer_reader.read_json(path) if path is not None else None

    def _read_session_index(self) -> dict[str, dict]:
        """The adapter-written rows for this scope, merged across shards."""
        return session_index.read_session_index_at(self._runtime_root)

    # ── The reads the BFF routes make ────────────────────────────────

    def read_stats(self) -> Optional[dict]:
        return self._read_runtime_json(self._STATS)

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
        path = self._runtime_path(self._TIMELINE)
        if path is None:
            return []
        return visualizer_reader.read_jsonl_tail_reverse(
            path,
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
        base = self._read_session_index()
        aug = self._read_runtime_json(self._SESSIONS_AUG)
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
    agent-facing ``POST/PATCH/DELETE /todos`` endpoints. Those endpoints
    are the file's **only** writer, for every backend — the watcher's
    todos sink is gone (syncplan §7, T8). The CRUD helpers still take
    :func:`visualizer.flock.locked`, but against themselves: two
    concurrent requests are two read-modify-writes on one document.
    """

    def __init__(self, project_id: str) -> None:
        self.project_id = project_layout.resolve_project_dirname(project_id)
        self._xo_root = project_layout.xo_dir(self.project_id)
        # One JSON read per construction: the runtime root is keyed by
        # ``project.json:pid``, so resolving it means reading that file. It
        # comes back ``None`` for a project that does not exist and for one
        # whose identity has not been minted yet — both of which are an
        # empty-reading scope, never an error. That is what keeps a
        # pid-less project a 200-with-zeros instead of a 500.
        self._runtime_root = project_layout.runtime_dir_for_project(self.project_id)
        self._activity_path = watcher_state.project_activity_path(self.project_id)

    def _read_session_index(self) -> dict[str, dict]:
        """Adapter rows for this project, with the pre-T19 read-through.

        A project that has produced no session since the move still has its
        rows in ``<project>/.xo/sessions/`` — they surface here until the
        first shard write supersedes them.
        """
        return session_index.read_session_index_at(
            self._runtime_root, legacy_root=self._xo_root
        )

    def _read_runtime_json(self, relative: str) -> Optional[dict]:
        doc = super()._read_runtime_json(relative)
        if doc is not None:
            return doc
        # Read-through to the pre-move copy (syncplan T19, migration). The
        # timeline is deliberately excluded — it is append-only with
        # rotation, and reconciling that glob across two roots is the
        # complexity open decision O3 chose not to buy.
        return visualizer_reader.read_json(self._xo_root / relative)

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
    """Read-only handle over the workspace tier (aggregate of all projects).

    Two roots, like every other scope since T19. The **synced** root is
    ``<XO root>/.xo/``, which after T20 holds only the workspace records; the
    **runtime** root is ``~/.quirq/workspace/``, which holds the rollups the
    watcher derives from a walk of every project (docs/syncplan.md §9, T20).
    Until the watcher's first tick, every read returns ``None`` / empty and
    the routes fall through to zero/empty payloads.
    """

    def __init__(self) -> None:
        self._xo_root = project_layout.workspace_xo_dir()
        # T20 split this tier the way T19 split the per-project one: the
        # derived rollups left ``<XO root>/.xo/`` for ``~/.quirq/workspace/``
        # and only the records stayed behind. Naming both roots here is what
        # lets the shared reader stay root-aware without a tier branch in
        # every method.
        self._runtime_root = project_layout.workspace_runtime_dir()
        self._activity_path = watcher_state.workspace_activity_path()

    def _read_session_index(self) -> dict[str, dict]:
        """The workspace union, which is one whole file.

        The per-project index is sharded because 15 adapter writers contend
        on it; this one has a single writer — the workspace sink, once a tick
        — so R-CONTEND's partitioning would buy nothing.
        """
        doc = visualizer_reader.read_json(
            project_layout.workspace_sessions_dir() / "sessionslist.json"
        )
        return doc if isinstance(doc, dict) else {}

    def read_projects(self) -> Optional[dict]:
        """``<XO root>/.xo/projects.json`` — the projects registry.

        Keyed by directory name, with ``pid`` a field and ``by_pid`` a
        derived reverse index (syncplan §5.2). Falls back to the retired
        ``workspace.json`` — a bare array of names — for one release, so
        a reader spanning the rename does not have to know which one is
        on disk.
        """
        doc = visualizer_reader.read_json(self._xo_root / "projects.json")
        if doc is None:
            doc = visualizer_reader.read_json(self._xo_root / "workspace.json")
        return doc

    def read_workspace(self) -> Optional[dict]:
        """Deprecated alias for :meth:`read_projects`. ``workspace.json``
        was renamed to ``projects.json`` and reshaped (syncplan §5.2)."""
        return self.read_projects()


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
