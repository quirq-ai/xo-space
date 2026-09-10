"""Centralised scope→handle resolution for the BFF layer."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Union

from services.cowork_agent import project_layout
from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.registry import agent_env
from services.cowork_agent.visualizer import peers_store
from services.cowork_agent.visualizer import reader as visualizer_reader
from services.cowork_agent.visualizer import todos_store
from services.cowork_agent.visualizer import workitem_claims
from services.cowork_agent.visualizer import workitems_store
from services.cowork_agent.visualizer import state as watcher_state

logger = logging.getLogger(__name__)


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
    """Shared read helpers for both visualizer scopes."""

    _xo_root: Path
    _runtime_root: Optional[Path]
    _activity_path: Path

    # Closed set of files the BFF endpoints read, each named against the root
    # that owns it.
    _TODOS:           str = "todos.json"          # synced
    _STATS:           str = "stats.json"          # runtime
    _TIMELINE:        str = "timeline.jsonl"      # runtime
    _SESSIONS_AUG:    str = "sessions/sessions-augment.json"   # runtime

    # ── Root-aware primitives ────────────────────────────────────────

    def _runtime_path(self, relative: str) -> Optional[Path]:
        """
        Resolve one runtime-tier file, or ``None`` when there is no runtime
        root to resolve it against.
        """
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
    """Read-only handle over ``<project>/.xo/`` for one project."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_layout.resolve_project_dirname(project_id)
        self._xo_root = project_layout.xo_dir(self.project_id)
        # One JSON read per construction: the runtime root is keyed by
        # ``project.json:pid``, so resolving it means reading that file.
        self._runtime_root = project_layout.runtime_dir_for_project(self.project_id)
        self._activity_path = watcher_state.project_activity_path(self.project_id)

    def _read_session_index(self) -> dict[str, dict]:
        """Adapter rows for this project, with the pre-T19 read-through."""
        return session_index.read_session_index_at(
            self._runtime_root, legacy_root=self._xo_root
        )

    def _read_runtime_json(self, relative: str) -> Optional[dict]:
        doc = super()._read_runtime_json(relative)
        if doc is not None:
            return doc
        # Read-through to the pre-move copy (syncplan T19, migration).
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
        return self._xo_root / "todos.json"

    def create_todo(self, **kwargs) -> dict:
        return todos_store.create_todo(self._todos_path(), **kwargs)

    def get_todo(self, todo_id: str):
        return todos_store.get_todo(self._todos_path(), todo_id)

    def update_todo(self, todo_id: str, **kwargs) -> dict:
        return todos_store.update_todo(self._todos_path(), todo_id, **kwargs)

    def delete_todo(self, todo_id: str, **kwargs) -> bool:
        # ``**kwargs`` like its three siblings above, and for the same reason:
        # the store takes ``deleted_by``, and a signature that dropped it made
        # the tombstone's attribution structurally unreachable over HTTP — the
        # field existed, nothing could set it.
        return todos_store.delete_todo(self._todos_path(), todo_id, **kwargs)

    # ── Workitems CRUD (delegates to visualizer.workitems_store) ──────
    # The sibling of the todos block above and deliberately the same shape:
    # ``workitems.json`` is the other authored document in the synced tier, the
    # routes are its only writer, and the store it delegates to raises the same
    # ``(code, message)`` error type.

    def _workitems_path(self):
        # Path stays behind the handle so route files never import pathlib (P2
        # grep stays clean), same as ``_todos_path``.
        return self._xo_root / "workitems.json"

    def create_workitem(self, **kwargs) -> dict:
        return workitems_store.create_workitem(self._workitems_path(), **kwargs)

    def get_workitem(self, workitem_id: str, **kwargs):
        return workitems_store.get_workitem(
            self._workitems_path(), workitem_id, **kwargs
        )

    def list_workitems(self, **kwargs) -> list[dict]:
        return workitems_store.list_workitems(self._workitems_path(), **kwargs)

    def update_workitem(self, workitem_id: str, **kwargs) -> dict:
        return workitems_store.update_workitem(
            self._workitems_path(), workitem_id, **kwargs
        )

    def delete_workitem(self, workitem_id: str, **kwargs) -> bool:
        return workitems_store.delete_workitem(
            self._workitems_path(), workitem_id, **kwargs
        )

    def adopt_workitem(self, **kwargs) -> tuple[dict, bool]:
        """Track a GitHub issue. Returns ``(record, created)``."""
        return workitems_store.adopt_workitem(self._workitems_path(), **kwargs)

    def unadopt_workitem(self, workitem_id: str, **kwargs) -> dict:
        """Stop mirroring the issue, keep the workitem."""
        return workitems_store.unadopt_workitem(
            self._workitems_path(), workitem_id, **kwargs
        )

    # ── Peers CRUD (delegates to visualizer.peers_store) ──────────────
    # The third authored document in the synced tier, and the same shape as the
    # two blocks above: the routes are its only writer and the store raises the
    # same ``(code, message)`` error type.

    def _peers_path(self):
        # Path stays behind the handle so route files never import pathlib (P2
        # grep stays clean), same as ``_todos_path``.
        return self._xo_root / "peers.json"

    def create_peer(self, **kwargs) -> dict:
        return peers_store.create_peer(self._peers_path(), **kwargs)

    def read_peer_roster(self, **kwargs) -> tuple:
        """``(updated_at, peers)`` — both in one read."""
        return peers_store.read_roster(self._peers_path(), **kwargs)

    def list_peers(self, **kwargs) -> list:
        return peers_store.list_peers(self._peers_path(), **kwargs)

    def get_peer(self, user_id: str, **kwargs):
        return peers_store.get_peer(self._peers_path(), user_id, **kwargs)

    def update_peer(self, user_id: str, **kwargs) -> dict:
        return peers_store.update_peer(self._peers_path(), user_id, **kwargs)

    def delete_peer(self, user_id: str, **kwargs) -> bool:
        return peers_store.delete_peer(self._peers_path(), user_id, **kwargs)

    # ── The GitHub mirror (runtime tier; read-only here) ──────────────
    # The other half of the read-time projection (§5.3).

    def read_github_mirror(self) -> Optional[dict]:
        """The project's GitHub issue mirror, or ``None``."""
        from services.cowork_agent.visualizer import github_mirror
        try:
            return github_mirror.read_mirror(self.project_id)
        except Exception:
            logger.warning(
                "could not read the github mirror for project %s; adopted "
                "workitems will render stale", self.project_id, exc_info=True,
            )
            return None

    def github_repo(self) -> Optional[str]:
        """``owner/name`` for this project's git remote, or ``None``."""
        from services.cowork_agent.connectors.github_issues import parse_remote_url
        meta = project_layout.load_project(self.project_id)
        git = meta.get("git") if isinstance(meta, dict) else None
        url = git.get("remote_url") if isinstance(git, dict) else None
        ref = parse_remote_url(url)
        if ref is None or not ref.is_github_com:
            return None
        return ref.slug

    # ── Workitem claims (runtime tier; derived in_progress) ───────────
    # The third tier this handle spans, and the one that must not be confused
    # with the second: ``workitems.json`` is authored state in the SYNCED root,
    # while a claim is machine-local, disposable runtime state (workitems-plan
    # §5.4, rule R-TIER).

    def _claims_path(self, *, create: bool = False):
        root = self._runtime_root
        if root is None and create:
            # The runtime home is resolved once at construction and can
            # legitimately be ``None`` (a project whose pid has not been minted
            # yet).
            root = project_layout.runtime_dir_for_project(
                self.project_id, create=True
            )
        if root is None:
            return None
        return workitem_claims.claims_path_for(root)

    def read_claims(self) -> dict:
        """Every claim on this project's workitems, or ``{}``."""
        path = self._claims_path()
        if path is None:
            return {}
        return workitem_claims.read_claims_quiet(path)

    def claim_workitem(self, workitem_id: str, **kwargs) -> dict:
        path = self._claims_path(create=True)
        if path is None:
            raise workitem_claims.WorkitemClaimsError(
                "scope_unavailable",
                "this project has no runtime home, so a claim cannot be "
                "recorded; it is created on first write and could not be.",
            )
        return workitem_claims.claim_workitem(path, workitem_id, **kwargs)

    def release_workitem(self, workitem_id: str) -> bool:
        path = self._claims_path()
        if path is None:
            return False
        return workitem_claims.release_workitem(path, workitem_id)

    def release_workitem_quiet(self, workitem_id: str) -> bool:
        """The implicit release — closing or deleting a workitem."""
        path = self._claims_path()
        if path is None:
            return False
        return workitem_claims.release_workitem_quiet(path, workitem_id)

    def in_progress_workitem_ids(self) -> frozenset[str]:
        """The workitems an agent is working **right now**, derived."""
        try:
            claims = self.read_claims()
            if not claims:
                return frozenset()
            live = workitem_claims.live_session_ids(self.read_activity())
            found = workitem_claims.in_progress_ids(claims, live_sessions=live)
            if len(found) < len(claims):
                # Presence rows carry the runtime's *native* session id, but a
                # claim may name the composite cowork key instead
                # (``links.session_ids`` uses that form, and so does the plan's
                # own example).
                widened = self._live_session_handles(live)
                if widened != live:
                    found = workitem_claims.in_progress_ids(
                        claims, live_sessions=widened
                    )
            return found
        except Exception:
            logger.warning(
                "could not derive in_progress for project %s; reporting none",
                self.project_id, exc_info=True,
            )
            return frozenset()

    def _live_session_handles(self, live: frozenset[str]) -> frozenset[str]:
        """``live`` plus every alternate handle for the same sessions."""
        if not live:
            return live
        try:
            rows = self.read_sessionslist()
        except Exception:
            return live
        widened = set(live)
        for key, row in rows.items():
            if not isinstance(row, dict):
                continue
            native = row.get("nativeSessionId")
            if native in live or key in live or row.get("sessionId") in live:
                widened.add(key)
                for handle in (native, row.get("sessionId")):
                    if isinstance(handle, str) and handle:
                        widened.add(handle)
        return frozenset(widened)


class WorkspaceVisualizerScope(_XoReader):
    """Read-only handle over the workspace tier (aggregate of all projects)."""

    def __init__(self) -> None:
        self._xo_root = project_layout.workspace_xo_dir()
        # T20 split this tier the way T19 split the per-project one: the
        # derived rollups left ``<XO root>/.xo/`` for ``~/.quirq/workspace/``
        # and only the records stayed behind.
        self._runtime_root = project_layout.workspace_runtime_dir()
        self._activity_path = watcher_state.workspace_activity_path()

    def _read_session_index(self) -> dict[str, dict]:
        """The workspace union, which is one whole file."""
        doc = visualizer_reader.read_json(
            project_layout.workspace_sessions_dir() / "sessionslist.json"
        )
        return doc if isinstance(doc, dict) else {}

    def read_projects(self) -> Optional[dict]:
        """``<XO root>/.xo/projects.json`` — the projects registry."""
        doc = visualizer_reader.read_json(self._xo_root / "projects.json")
        if doc is None:
            doc = visualizer_reader.read_json(self._xo_root / "workspace.json")
        return doc

    def read_workspace(self) -> Optional[dict]:
        """
        Deprecated alias for :meth:`read_projects`. ``workspace.json`` was
        renamed to ``projects.json`` and reshaped (syncplan §5.2).
        """
        return self.read_projects()

    # ── The workspace rollup (workitems-plan §7.3, W9) ────────────────

    def rollup_workitems(
        self,
        *,
        assignees: Optional[Iterable[str]] = None,
        status: Optional[str] = None,
    ) -> "WorkitemRollup":
        """Every project's workitems, **projected first and filtered after**."""
        from services.cowork_agent.visualizer import workitem_projection
        from services.cowork_agent.visualizer.workspace_index import (
            list_project_pids,
        )

        wanted: Optional[frozenset[str]] = None
        if assignees is not None:
            wanted = frozenset(
                value.casefold()
                for value in assignees
                if isinstance(value, str) and value
            )

        # One scan, both identities: the keys are the directory names
        # ``list_project_ids`` would return and the values are the pids.
        pids = list_project_pids()
        rows: list[dict] = []
        skipped: list[dict] = []
        for project_id in sorted(pids):
            pid = pids.get(project_id)
            try:
                project = VisualizerScope(project_id)
                stored = project.list_workitems()
            except Exception as exc:
                skipped.append({
                    "project_id": project_id,
                    "pid": pid,
                    "code": getattr(exc, "code", None) or "unavailable",
                    # The store's text names the absolute path.
                    "detail": str(exc),
                })
                continue
            issues = workitem_projection.mirror_issues(
                project.read_github_mirror()
            )
            matched = [
                record
                for record in workitem_projection.project_workitems(
                    stored, issues=issues
                )
                if _workitem_matches(record, assignees=wanted, status=status)
            ]
            if not matched:
                continue
            live = project.in_progress_workitem_ids()
            for record in matched:
                record["_project_id"] = project_id
                record["_pid"] = pid
                record["_in_progress"] = record.get("id") in live
                rows.append(record)
        return WorkitemRollup(rows=rows, projects=len(pids), skipped=skipped)


@dataclass(frozen=True)
class WorkitemRollup:
    """What :meth:`WorkspaceVisualizerScope.rollup_workitems` answers."""

    rows: list[dict]
    projects: int
    skipped: list[dict]


def _workitem_matches(
    record: dict, *, assignees: Optional[frozenset[str]], status: Optional[str],
) -> bool:
    """The rollup's predicate, applied to a **projected** record."""
    if status is not None and record.get("status") != status:
        return False
    if assignees is None:
        return True
    found = {
        value.casefold()
        for key in ("assignees", "github_assignees")
        for value in record.get(key) or []
        if isinstance(value, str) and value
    }
    single = record.get("assignee")
    if isinstance(single, str) and single:
        # The projection fills ``assignees`` from it, so this is belt and
        # braces — and it is what keeps the predicate correct on a record that
        # never went through the projection at all.
        found.add(single.casefold())
    return bool(found & assignees)


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
