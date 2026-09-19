"""The projects module's facade: what the routes, the CLI commands and other
modules call.

Two handles and a resolver, moved here from ``services.cowork_agent.scopes``
(which still exports them, so every importer and every patch through the
old path reaches these objects):

* :class:`VisualizerScope`: one project. Reads its committed records
  (``.xo/todos.json``, ``workitems.json``, ``peers.json``), its runtime
  files (stats, the session index, the GitHub mirror, the workitem claims,
  the presence snapshot) and its timeline (through ``modules.timeline``);
  writes the records through the stores, which are the event source.
* :class:`WorkspaceVisualizerScope`: the workspace tier, and the workitem
  rollup across every project.
* :func:`resolve_scope`: ``"xo-projects"`` (the projects root),
  ``"xo-projects-visualizer"`` (a project) and
  ``"xo-workspace-visualizer"``; ``scopes.resolve_scope`` adds ``"secrets"``.

Plus the reads and writes the routes used to make themselves: the project
list, a project's tree and file preview, a file's git history, the clone
and removal of a local project (``project_management``) and the two graph
views (``/xo/space.json``, ``/xo/dashboard.json``). Knows nothing about
HTTP: a failure is a :class:`ServiceError` carrying its status.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

import modules.timeline.service as timeline_service
from services.cowork_agent import project_layout
from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.file_history import file_git_history, read_file_at_commit
from services.cowork_agent.visualizer import reader as visualizer_reader
from services.cowork_agent.visualizer import state as watcher_state
from services.cowork_agent.visualizer.workspace import views as workspace_views
from services.errors import NotFound, ServiceError, Unavailable

from . import (  # noqa: F401  (project_management is re-exported for callers that patch it)
    github_mirror,
    peers_store,
    project_management,
    todos_store,
    workitem_claims,
    workitem_projection,
    workitems_store,
)
from .project_management import clone_project, remove_project, removal_status  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)


class ScopeNotFound(Exception):
    """Raised when a caller asks for a scope name we don't recognise."""


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
    _SESSIONS_AUG:    str = "sessions/sessions-augment.json"   # runtime
    # The timeline (``timeline.jsonl``) is read through ``modules.timeline``.

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
        """Newest first, through ``modules.timeline``; each scope says which log."""
        raise NotImplementedError

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

    def read_timeline(
        self,
        *,
        limit: int,
        before: Optional[str] = None,
        types: Optional[frozenset[str]] = None,
    ) -> list[dict]:
        """This project's log, ``~/.quirq/projects/<pid>/timeline.jsonl``:
        the runtime home's folder name is the pid (or the folder name while
        the project has none)."""
        if self._runtime_root is None:
            return []
        return timeline_service.read(limit=limit, before=before, types=types,
                                     pid=self._runtime_root.name)

    def project_exists(self) -> bool:
        """Whether the project directory exists on disk.

        Used by routes to distinguish 404 ``project_not_found`` from
        empty-state ``200 {…: zeros}``.
        """
        return project_layout.project_dir_exists(self.project_id)

    # ── Todos CRUD (delegates to todos_store) ─────────────────────────

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
        # the tombstone's attribution structurally unreachable over HTTP; the
        # field existed, nothing could set it.
        return todos_store.delete_todo(self._todos_path(), todo_id, **kwargs)

    # ── Workitems CRUD (delegates to workitems_store) ─────────────────
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

    # ── Peers CRUD (delegates to peers_store) ─────────────────────────
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
        """``(updated_at, peers)``, both in one read."""
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
        from services.cowork_agent.connectors.github.issues import parse_remote_url
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
                "scope_unavailable", "the workitem claim could not be recorded.", 500,
                log="this project has no runtime home, so a claim cannot be "
                    "recorded; it is created on first write and could not be.",
            )
        return workitem_claims.claim_workitem(path, workitem_id, project_id=self.project_id, **kwargs)

    def release_workitem(self, workitem_id: str) -> bool:
        path = self._claims_path()
        if path is None:
            return False
        return workitem_claims.release_workitem(path, workitem_id, project_id=self.project_id)

    def release_workitem_quiet(self, workitem_id: str) -> bool:
        """The implicit release: closing or deleting a workitem."""
        path = self._claims_path()
        if path is None:
            return False
        return workitem_claims.release_workitem_quiet(path, workitem_id, project_id=self.project_id)

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
        # derived rollups left ``<XO root>/.xo/`` for ``~/.quirq/cache/``
        # and only the records stayed behind.
        self._runtime_root = project_layout.workspace_runtime_dir()
        self._activity_path = watcher_state.workspace_activity_path()

    def read_timeline(
        self,
        *,
        limit: int,
        before: Optional[str] = None,
        types: Optional[frozenset[str]] = None,
    ) -> list[dict]:
        """The merged Space view: every project's log plus the Space log
        (``~/.quirq/projects/timeline.jsonl``), newest first, each line
        tagged with its project's folder name."""
        return timeline_service.read(limit=limit, before=before, types=types)

    def _read_session_index(self) -> dict[str, dict]:
        """The workspace union, which is one whole file."""
        doc = visualizer_reader.read_json(
            project_layout.workspace_sessions_dir() / "sessionslist.json"
        )
        return doc if isinstance(doc, dict) else {}

    def read_projects(self) -> Optional[dict]:
        """``<XO root>/.xo/projects.json``: the projects registry."""
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
                    # The store keeps the path-naming text in ``log``; it is
                    # for the server log, never the wire.
                    "detail": getattr(exc, "log", None) or str(exc),
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
        # braces, and it is what keeps the predicate correct on a record that
        # never went through the projection at all.
        found.add(single.casefold())
    return bool(found & assignees)


# ── Resolver ──────────────────────────────────────────────────────────────────


ScopeHandle = Union[Path, VisualizerScope, WorkspaceVisualizerScope]


def resolve_scope(name: str, *args) -> ScopeHandle:
    """Resolve a project scope name to its handle.

    Returns the projects root ``Path`` for ``"xo-projects"``, a
    :class:`VisualizerScope` for ``"xo-projects-visualizer"`` (which takes a
    ``project_id``) or a :class:`WorkspaceVisualizerScope` for
    ``"xo-workspace-visualizer"``. The ``"secrets"`` scope belongs to
    settings: ``services.cowork_agent.scopes.resolve_scope`` answers it and
    delegates the rest here.
    """
    if name == "xo-projects":
        return project_layout.xo_projects_root()
    if name == "xo-projects-visualizer":
        if len(args) != 1 or not isinstance(args[0], str):
            raise ScopeNotFound(
                "xo-projects-visualizer requires a project_id string argument"
            )
        return VisualizerScope(args[0])
    if name == "xo-workspace-visualizer":
        return WorkspaceVisualizerScope()
    raise ScopeNotFound(f"Unknown scope: {name!r}")


def require_project(project_id: str) -> VisualizerScope:
    """A project's handle, or 404 ``project_not_found``."""
    scope = VisualizerScope(project_id)
    if not scope.project_exists():
        raise NotFound("project_not_found", "Project not found.")
    return scope


def workspace() -> WorkspaceVisualizerScope:
    return WorkspaceVisualizerScope()


# ── The project list ──────────────────────────────────────────────────────────

#: Folders under the projects root that are the workspace's own, never a
#: project (the same set ``routers/cowork_agent/bff/filters.py`` names as
#: ``PROJECT_SYSTEM_LEAVES``; restated here because this facade imports no
#: router module).
SYSTEM_LEAVES: frozenset[str] = frozenset({"agents", "memory", "state", "projects"})

# A project's own docs are the only place a human sentence about it exists:
# .xo/project.json's description is empty for every project the API did not
# create. Read the first real line of the usual suspects so the list can say
# what a project IS, not just when it was created. Bounded: 3 files, 2 KB
# each, first 40 lines.
#
# AGENTS.md is deliberately NOT in this list: it is the scaffold's operating
# contract, so its opening line is identical in every project and would put
# the same sentence on every row, the exact failure this replaces.
_DESC_FILES = ("README.md", "PROJECT.md", "OBJECTIVES.md")
_DESC_MAX = 160


def _to_iso_utc(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def described(project_id: str) -> Optional[str]:
    """The first paragraph of the project's README (or PROJECT.md,
    OBJECTIVES.md), as plain text, or ``None``."""
    root = project_layout.project_dir(project_id)
    for candidate in _DESC_FILES:
        path = root / candidate
        try:
            if not path.is_file():
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                head = fh.read(2048)
        except OSError:
            continue
        # Take the whole first paragraph, not the first line: markdown wraps
        # at ~80 columns, so one line ends mid-sentence ("…in the tradition
        # of") and reads like a truncation bug.
        para: list[str] = []
        for line in head.splitlines()[:40]:
            text = line.strip()
            if not text:
                if para:
                    break
                continue
            # skip headings, badges, html, front matter, tables, quotes, code
            if text.startswith(("#", "!", "<", "---", "|", ">", "```")):
                if para:
                    break
                continue
            if text.startswith(("- ", "* ", "+ ")):
                if para:
                    break
                continue
            para.append(text)
        if not para:
            continue
        text = " ".join(" ".join(para).split())
        # The row shows plain text, so strip the markdown that survives a
        # raw paragraph grab: links, emphasis, code ticks.
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = text.replace("**", "").replace("`", "").strip("*_ ")
        if len(text) < 12:
            continue
        if len(text) > _DESC_MAX:
            cut = text[:_DESC_MAX].rsplit(" ", 1)[0].rstrip(" ,;:.")
            text = cut + "…"
        return text
    return None


def _shape_scaffolded(entry: dict) -> dict:
    name = str(entry.get("name") or "")
    display = entry.get("display_name") or name
    return {
        "id": name,
        "display_name": str(display),
        "description": entry.get("description") or described(name) or None,
        "created_at": entry.get("created_at") or None,
        "unscaffolded": False,
    }


def _shape_unscaffolded(entry: dict) -> dict:
    name = str(entry.get("name") or "")
    return {
        "id": name,
        "display_name": name,
        "description": None,
        "created_at": _to_iso_utc(entry.get("mtime")),
        "unscaffolded": True,
    }


def _sort_newest_first(items: list[dict]) -> list[dict]:
    """Newest first by created_at; nulls last; alphabetical tiebreak."""
    with_ts = sorted([p for p in items if p["created_at"]], key=lambda p: p["id"])
    with_ts.sort(key=lambda p: p["created_at"] or "", reverse=True)
    without_ts = sorted([p for p in items if not p["created_at"]], key=lambda p: p["id"])
    return with_ts + without_ts


def list_projects() -> list[dict]:
    """Every project under the projects root, scaffolded and bare, newest
    first: ``{id, display_name, description, created_at, unscaffolded}``
    per row, never a path. The workspace's own folders are left out."""
    try:
        project_layout.xo_projects_root()
        scaffolded = project_layout.list_projects()
        unscaffolded = project_layout.list_unscaffolded_dirs()
    except OSError as exc:
        raise ServiceError("scope_unavailable", "Project directory is not readable.", 500,
                           log=f"projects root: {exc}") from exc
    items: list[dict] = []
    for entry in scaffolded:
        if (entry.get("name") or "") in SYSTEM_LEAVES:
            continue
        items.append(_shape_scaffolded(entry))
    for entry in unscaffolded:
        if (entry.get("name") or "") in SYSTEM_LEAVES:
            continue
        items.append(_shape_unscaffolded(entry))
    return _sort_newest_first(items)


# ── A project's tree, a file, its history ─────────────────────────────────────

PREVIEW_MAX_BYTES = 256 * 1024
# Text this UI can present honestly: rendered markdown, sandboxed HTML, and
# plain text. Anything else (an image, a 40 MB parquet) gets a "no preview"
# state from its metadata rather than a wall of replacement characters.
PREVIEW_SUFFIXES = frozenset(
    {
        ".md", ".markdown", ".mdx",
        ".html", ".htm",
        ".txt", ".text", ".rst", ".log",
        ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env.example",
        ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".sh", ".sql",
        ".go", ".rs", ".java", ".c", ".h", ".cpp",
    }
)
HISTORY_MAX_COMMITS = 200
HISTORY_DEFAULT_COMMITS = 50


def preview_kind(name: str) -> str:
    lower = name.lower()
    if lower.endswith((".md", ".markdown", ".mdx")):
        return "markdown"
    if lower.endswith((".html", ".htm")):
        return "html"
    return "text"


def _require_project_dir(project_id: str) -> None:
    if not project_layout.project_dir_exists(project_id):
        raise NotFound("project_not_found", "Project not found.")


def _bad_relative_path(exc: Exception) -> ServiceError:
    return ServiceError("invalid_relative_path",
                        "relative_path is malformed or escapes the project root.", 400,
                        log=str(exc))


def _unreadable(exc: Exception, what: str) -> ServiceError:
    return ServiceError("scope_unavailable", f"{what} is not readable.", 500, log=str(exc))


def project_tree(project_id: str, relative_path: str = "") -> dict:
    """One level of a project's folder, as ``project_layout.list_project_tree``
    shapes it: ``{project_id, relative_path, parent_relative_path, dirs,
    files}`` with raw entries (the routes hide dotfiles and shape rows).
    404 ``project_not_found`` / ``directory_not_found``, 400
    ``invalid_relative_path``, 500 ``scope_unavailable``."""
    _require_project_dir(project_id)
    try:
        raw = project_layout.list_project_tree(project_id, relative_path)
    except ValueError as exc:
        raise _bad_relative_path(exc) from exc
    except OSError as exc:
        raise _unreadable(exc, "Project directory") from exc
    if raw is None:
        raise NotFound("directory_not_found", "Directory not found in project.")
    return raw


def project_file(project_id: str, relative_path: str, *, commit: Optional[str] = None,
                 commit_path: Optional[str] = None) -> dict:
    """One previewable text file: ``{project_id, relative_path, name, kind,
    size_bytes, modified_at, truncated, content}``. With ``commit`` (and,
    across renames, ``commit_path``) the content comes from that commit.
    415 ``preview_unsupported`` for a suffix the UI cannot present."""
    _require_project_dir(project_id)
    # The gate judges the name the content will carry: the historical name
    # when a version is asked for, today's name otherwise.
    gate_name = commit_path if (commit and commit_path) else relative_path
    suffix = project_layout.relative_path_suffix(gate_name or "").lower()
    if suffix not in PREVIEW_SUFFIXES:
        raise ServiceError("preview_unsupported",
                           f"No text preview for {suffix or 'this file type'}.", 415)
    if commit is not None:
        return _file_at_commit(project_id, relative_path, commit, commit_path)
    try:
        raw = project_layout.read_project_file(project_id, relative_path, max_bytes=PREVIEW_MAX_BYTES)
    except ValueError as exc:
        raise _bad_relative_path(exc) from exc
    except OSError as exc:
        raise _unreadable(exc, "File") from exc
    if raw is None:
        raise NotFound("file_not_found", "File not found in project.")
    return {
        "project_id": raw["project_id"],
        "relative_path": raw["relative_path"],
        "name": raw["name"],
        "kind": preview_kind(raw["name"]),
        "size_bytes": raw["size_bytes"],
        "modified_at": _to_iso_utc(raw["modified_at"]),
        "truncated": raw["truncated"],
        "content": raw["content"],
    }


def _file_at_commit(project_id: str, relative_path: str, commit: str, commit_path: Optional[str]) -> dict:
    try:
        raw = read_file_at_commit(project_id, relative_path, commit, commit_path=commit_path)
    except ValueError as exc:
        raise ServiceError("invalid_version_request", "commit or commit_path is malformed.", 400,
                           log=str(exc)) from exc
    except OSError as exc:
        raise _unreadable(exc, "File") from exc
    if raw is None:
        raise NotFound("file_not_found", "File not found in project.")
    if raw["content"] is None:
        raise NotFound("version_not_found",
                       "No repository owns this file." if not raw["is_repo"]
                       else "This commit has no version of the file.")
    return {
        "project_id": raw["project_id"],
        "relative_path": raw["relative_path"],
        "name": raw["name"],
        "kind": preview_kind(raw["name"]),
        "size_bytes": len(raw["content"].encode("utf-8")),
        "modified_at": None,
        "truncated": raw["truncated"],
        "content": raw["content"],
    }


def file_history(project_id: str, relative_path: str, *, limit: int = HISTORY_DEFAULT_COMMITS) -> dict:
    """The git log of one project file's edits, newest first:
    ``{project_id, relative_path, is_repo, items}``."""
    _require_project_dir(project_id)
    try:
        raw = file_git_history(project_id, relative_path, limit=max(1, min(int(limit), HISTORY_MAX_COMMITS)))
    except ValueError as exc:
        raise _bad_relative_path(exc) from exc
    except OSError as exc:
        raise _unreadable(exc, "File") from exc
    if raw is None:
        raise NotFound("file_not_found", "File not found in project.")
    return raw


# ── The records, read for the routes and the CLI ──────────────────────────────


def project_todos(project_id: str) -> Optional[dict]:
    """The raw ``todos.json`` of a project (``None`` when absent); an
    unreadable file is 500 ``scope_unavailable``."""
    scope = require_project(project_id)
    try:
        return scope.read_todos()
    except Exception as exc:  # malformed JSON: fail closed
        raise ServiceError("scope_unavailable", "todos.json is not readable.", 500, log=str(exc)) from exc


def project_activity(project_id: str) -> Optional[dict]:
    """The watcher's presence snapshot for a project (``None`` before the
    first tick); an unreadable file is 500 ``scope_unavailable``."""
    scope = require_project(project_id)
    try:
        return scope.read_activity()
    except Exception as exc:
        raise ServiceError("scope_unavailable", "activity state is not readable.", 500, log=str(exc)) from exc


def project_workitems(project_id: str, **filters) -> list[dict]:
    """A project's workitems, projected against its GitHub mirror, oldest
    first, with ``in_progress`` derived from the live claims."""
    scope = require_project(project_id)
    rows = scope.list_workitems(**filters)
    live = scope.in_progress_workitem_ids()
    issues = workitem_projection.mirror_issues(scope.read_github_mirror())
    out = []
    for row in rows:
        record = workitem_projection.project_workitem(row, issues=issues)
        record["in_progress"] = row.get("id") in live
        out.append(record)
    return out


# ── The graph views ───────────────────────────────────────────────────────────

VIEW_MAX_AGE_S = float(os.getenv("XO_VIEW_MAX_AGE_S", "120"))

_UNAVAILABLE = {
    "space": ("space_unavailable", "Could not build the workspace graph."),
    "dashboard": ("dashboard_unavailable", "Could not build the categorized project graph."),
}


async def view_payload(name: str) -> dict:
    """The ``space`` or ``dashboard`` view: the file under the cache when it
    is fresh, else a rebuild off the event loop; a stale file beats an
    absent one when the rebuild fails. 503 when there is nothing to serve."""
    if name not in _UNAVAILABLE:
        raise NotFound("view_not_found", f"No view named {name!r}.")
    try:
        # ``stale_ok``: keep the payload even when it is past the window, so a
        # failed rebuild has something correct to fall back on.
        payload, age = workspace_views.read(name, max_age_s=VIEW_MAX_AGE_S, stale_ok=True)
    except Exception as exc:  # noqa: BLE001 - unreadable file: fall through to a rebuild
        logger.warning("%s view unreadable (%s)", name, exc)
        payload, age = None, None
    if payload is None or workspace_views.is_stale(age, VIEW_MAX_AGE_S):
        try:
            rebuilt = await asyncio.to_thread(workspace_views.build, name)
        except Exception as exc:  # noqa: BLE001 - the walk failed; the file may still be good
            logger.warning("%s view rebuild failed (%s)", name, exc)
            rebuilt = None
        if rebuilt is not None:
            payload = rebuilt
        elif payload is not None:
            # Stale beats absent, and beats a 503 even harder: the walk failed,
            # the file on disk is still a valid graph, and the client wants a
            # graph.
            logger.warning("%s view rebuild produced nothing; serving age=%s", name, age)
    if payload is None:
        code, message = _UNAVAILABLE[name]
        raise Unavailable(code, message)
    return payload
