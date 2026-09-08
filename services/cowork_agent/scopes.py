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
  todo CRUD methods, ``workitems.json`` and its seven (five CRUD plus the
  adopt/unadopt transitions), plus the workspace registry;
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

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Union

from services.cowork_agent import project_layout
from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.registry import agent_env
from services.cowork_agent.visualizer import reader as visualizer_reader
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
    agent-facing ``POST/PATCH/DELETE /todos`` endpoints, and the same
    over ``.xo/workitems.json`` for ``/workitems`` (workitems-plan §7.1). Those endpoints
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

    def delete_todo(self, todo_id: str, **kwargs) -> bool:
        # ``**kwargs`` like its three siblings above, and for the same
        # reason: the store takes ``deleted_by``, and a signature that
        # dropped it made the tombstone's attribution structurally
        # unreachable over HTTP — the field existed, nothing could set it.
        from services.cowork_agent.visualizer import todos_store
        return todos_store.delete_todo(self._todos_path(), todo_id, **kwargs)

    # ── Workitems CRUD (delegates to visualizer.workitems_store) ──────
    #
    # The sibling of the todos block above and deliberately the same
    # shape: ``workitems.json`` is the other authored document in the
    # synced tier, the routes are its only writer, and the store it
    # delegates to raises the same ``(code, message)`` error type. Every
    # method forwards ``**kwargs`` for the O-A reason spelled out above —
    # ``delete_workitem``'s ``deleted_by`` is the exact field that was
    # unreachable for todos, and a signature that named its arguments
    # here would reintroduce the defect on a second document.

    def _workitems_path(self):
        # Path stays behind the handle so route files never import
        # pathlib (P2 grep stays clean), same as ``_todos_path``.
        from services.cowork_agent.visualizer import workitems_store  # noqa: F401
        return self._xo_root / "workitems.json"

    def create_workitem(self, **kwargs) -> dict:
        from services.cowork_agent.visualizer import workitems_store
        return workitems_store.create_workitem(self._workitems_path(), **kwargs)

    def get_workitem(self, workitem_id: str, **kwargs):
        from services.cowork_agent.visualizer import workitems_store
        return workitems_store.get_workitem(
            self._workitems_path(), workitem_id, **kwargs
        )

    def list_workitems(self, **kwargs) -> list[dict]:
        from services.cowork_agent.visualizer import workitems_store
        return workitems_store.list_workitems(self._workitems_path(), **kwargs)

    def update_workitem(self, workitem_id: str, **kwargs) -> dict:
        from services.cowork_agent.visualizer import workitems_store
        return workitems_store.update_workitem(
            self._workitems_path(), workitem_id, **kwargs
        )

    def delete_workitem(self, workitem_id: str, **kwargs) -> bool:
        from services.cowork_agent.visualizer import workitems_store
        return workitems_store.delete_workitem(
            self._workitems_path(), workitem_id, **kwargs
        )

    def adopt_workitem(self, **kwargs) -> tuple[dict, bool]:
        """Track a GitHub issue. Returns ``(record, created)``.

        A *state transition*, not a field edit — ``source.kind`` decides
        which fields the record may carry at all — which is why it is a
        method of its own rather than another ``update_workitem`` call
        (workitems-plan §13, amendment 8).
        """
        from services.cowork_agent.visualizer import workitems_store
        return workitems_store.adopt_workitem(self._workitems_path(), **kwargs)

    def unadopt_workitem(self, workitem_id: str, **kwargs) -> dict:
        """Stop mirroring the issue, keep the workitem.

        The four GitHub-owned fields are materialised in the same write
        that drops ``source.github``: the schema *requires* ``status`` on
        a local record and forbids it on an adopted one, so the two
        halves cannot be separate calls without leaving an invalid
        document in between.
        """
        from services.cowork_agent.visualizer import workitems_store
        return workitems_store.unadopt_workitem(
            self._workitems_path(), workitem_id, **kwargs
        )

    # ── The GitHub mirror (runtime tier; read-only here) ──────────────
    #
    # The other half of the read-time projection (§5.3). The poller is the
    # mirror's single writer and this handle deliberately exposes no way
    # to change that: a route that wrote a freshly-assigned login into the
    # mirror to save a UI 60 seconds of staleness would make the document
    # two-writer, and the merge rules in ``github_mirror`` assume it is
    # not. The honest answer to "the mirror has not caught up yet" is to
    # say so, not to forge the row.

    def read_github_mirror(self) -> Optional[dict]:
        """The project's GitHub issue mirror, or ``None``.

        ``None`` covers every unusable state — never polled, no runtime
        home, unreadable bytes, a schema this revision does not write —
        because they are one state to a reader: GitHub has told us
        nothing, so adopted items project as stale.
        """
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
        """``owner/name`` for this project's git remote, or ``None``.

        Read from the durable ``project.json:git.remote_url`` — the same
        input the poller uses, so a project the poller polls and a project
        the adoption route can reach are the same set by construction.
        ``None`` for no remote, an unparseable one, or a host that is not
        github.com: an Enterprise or GitLab remote is not a repository any
        of these calls could resolve, and saying so costs nothing.
        """
        from services.cowork_agent.connectors.github_issues import parse_remote_url
        meta = project_layout.load_project(self.project_id)
        git = meta.get("git") if isinstance(meta, dict) else None
        url = git.get("remote_url") if isinstance(git, dict) else None
        ref = parse_remote_url(url)
        if ref is None or not ref.is_github_com:
            return None
        return ref.slug

    # ── Workitem claims (runtime tier; derived in_progress) ───────────
    #
    # The third tier this handle spans, and the one that must not be
    # confused with the second: ``workitems.json`` is authored state in
    # the SYNCED root, while a claim is machine-local, disposable
    # runtime state (workitems-plan §5.4, rule R-TIER). Resolving the
    # path here — through ``runtime_dir_for_project``, never by hand —
    # is what keeps a claim structurally incapable of reaching ``.xo/``,
    # and it is also why two Spaces working the same GitHub issue each
    # see only their own agent's progress.

    def _claims_path(self, *, create: bool = False):
        from services.cowork_agent.visualizer import workitem_claims
        root = self._runtime_root
        if root is None and create:
            # The runtime home is resolved once at construction and can
            # legitimately be ``None`` (a project whose pid has not been
            # minted yet). A read answers empty for that; a write asks
            # for it to be created rather than silently dropping a claim.
            root = project_layout.runtime_dir_for_project(
                self.project_id, create=True
            )
        if root is None:
            return None
        return workitem_claims.claims_path_for(root)

    def read_claims(self) -> dict:
        """Every claim on this project's workitems, or ``{}``.

        Total by construction — an unwritable or unreadable claims file
        reads as "nothing is claimed" rather than raising, because this
        feeds a derived display field and not a decision.
        """
        from services.cowork_agent.visualizer import workitem_claims
        path = self._claims_path()
        if path is None:
            return {}
        return workitem_claims.read_claims_quiet(path)

    def claim_workitem(self, workitem_id: str, **kwargs) -> dict:
        from services.cowork_agent.visualizer import workitem_claims
        path = self._claims_path(create=True)
        if path is None:
            raise workitem_claims.WorkitemClaimsError(
                "scope_unavailable",
                "this project has no runtime home, so a claim cannot be "
                "recorded; it is created on first write and could not be.",
            )
        return workitem_claims.claim_workitem(path, workitem_id, **kwargs)

    def release_workitem(self, workitem_id: str) -> bool:
        from services.cowork_agent.visualizer import workitem_claims
        path = self._claims_path()
        if path is None:
            return False
        return workitem_claims.release_workitem(path, workitem_id)

    def release_workitem_quiet(self, workitem_id: str) -> bool:
        """The implicit release — closing or deleting a workitem.

        Never raises: the workitem write has already happened, and a
        claim left behind lapses with its session anyway.
        """
        from services.cowork_agent.visualizer import workitem_claims
        path = self._claims_path()
        if path is None:
            return False
        return workitem_claims.release_workitem_quiet(path, workitem_id)

    def in_progress_workitem_ids(self) -> frozenset[str]:
        """The workitems an agent is working **right now**, derived.

        Nothing is stored and nothing is cleaned up: a claim whose
        session has left ``open_sessions`` simply stops being reported,
        which is why killing the agent process clears ``in_progress``
        with no cleanup path to write, forget, or get wrong.

        Total — any failure answers "none in progress", because this is
        one field on a list that must keep rendering.
        """
        from services.cowork_agent.visualizer import workitem_claims
        try:
            claims = self.read_claims()
            if not claims:
                return frozenset()
            live = workitem_claims.live_session_ids(self.read_activity())
            found = workitem_claims.in_progress_ids(claims, live_sessions=live)
            if len(found) < len(claims):
                # Presence rows carry the runtime's *native* session id,
                # but a claim may name the composite cowork key instead
                # (``links.session_ids`` uses that form, and so does the
                # plan's own example). Widen the live set through the
                # session index only when some claim went unmatched, so
                # the common case still costs one JSON read.
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
        """``live`` plus every alternate handle for the same sessions.

        ``read_one_session`` already treats the composite key, the
        ``nativeSessionId`` and the inner ``sessionId`` as three handles
        on one session; a claim naming any of them must resolve to the
        same liveness answer as a claim naming the native id.
        """
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

    # ── The workspace rollup (workitems-plan §7.3, W9) ────────────────

    def rollup_workitems(
        self,
        *,
        assignees: Optional[Iterable[str]] = None,
        status: Optional[str] = None,
    ) -> "WorkitemRollup":
        """Every project's workitems, **projected first and filtered after**.

        This is the one thing the per-project route deliberately cannot do.
        ``workitems_store.list_workitems``'s ``status`` and ``assignee``
        filters read what is *stored*, so an adopted item never matches
        either — it stores neither field, because GitHub owns both (§5.3).
        Filtering there would silently drop every adopted item, which is
        why it does not try. Here the join runs **before** the predicate:
        each project's records are read, joined with that project's GitHub
        mirror through :mod:`visualizer.workitem_projection`, and the
        *result* is filtered. An adopted item therefore matches on the
        mirror's status and the mirror's assignees, which is the only place
        those two facts exist.

        **No network, ever.** The mirror is a local JSON file the poller
        owns; nothing here fetches, and nothing here writes. A project the
        poller has never reached simply projects stale, and a stale row
        carries ``status`` / ``assignee`` ``None`` — so it matches neither
        ``?status=`` nor ``?assignee=``, and appears only in an unfiltered
        rollup. Absent beats wrong (§5.3), applied to a predicate.

        **Cost per call**: one scan of the projects root — the scan
        :func:`list_project_ids` already makes, asked for its pid
        projection instead, so both identities come out of it for the
        price of one — and then, per project, one ``project.json`` read to
        resolve the runtime home, one ``.xo/workitems.json`` read and one
        ``github/issues.json`` read. The claim and presence reads behind
        ``in_progress`` are taken **only for a project that still has a
        matching row** after filtering, so a narrow query does not pay for
        the whole workspace. The projection itself is pure — no I/O at
        all, and no network anywhere on the path.

        **Totality is the point.** This is what an agent polls, so one
        malformed project must not take the answer down. A project whose
        ``workitems.json`` is corrupt, whose schema is from the future, or
        whose directory has gone away between the walk and the read is
        recorded in :attr:`WorkitemRollup.skipped` and left out of
        ``rows`` — never raised. ``skipped`` carries the store's own code
        and message so the caller can decide how loudly to say so;
        rendering it is the route's job, and so is keeping the absolute
        path in the message out of the response.

        ``assignees`` matches case-insensitively against the *projected*
        ``assignees`` list (and the singular ``assignee``): GitHub logins
        are unique case-insensitively and a caller typing ``@Octocat``
        means the same person GitHub spells ``octocat``. ``None`` means no
        assignee filter; an **empty** iterable is a filter nothing can
        match, which is the honest answer when "me" resolved to nobody.
        """
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
        # ``project_index_scope`` is deliberately *not* entered — its
        # contract is a per-tick memo for code that asks several times, and
        # this asks once, so it would memoize a scan that already happens
        # exactly once while making a request-path caller reason about
        # staleness it does not otherwise have.
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
                    # The store's text names the absolute path. It is carried
                    # for the caller to log, never to serve — the route blanks
                    # it for the same reason ``_shape_todos`` blanks
                    # ``source_file``.
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
    """What :meth:`WorkspaceVisualizerScope.rollup_workitems` answers.

    ``rows`` is a **flat list**, never a map keyed by anything: O-C was a
    cross-project union keyed by a constant that silently kept one
    project's row and lost the rest, and a list has no union key, so that
    class of defect cannot occur here at all (§7.3, D4). Each row is a
    projected record tagged with ``_project_id``, ``_pid`` and
    ``_in_progress`` — underscore-prefixed because they are not fields of
    the stored document and must not be mistaken for them.

    ``projects`` counts what was *walked*, not what answered, so
    ``projects`` minus ``len(skipped)`` is what was actually read.
    """

    rows: list[dict]
    projects: int
    skipped: list[dict]


def _workitem_matches(
    record: dict, *, assignees: Optional[frozenset[str]], status: Optional[str],
) -> bool:
    """The rollup's predicate, applied to a **projected** record.

    Reads ``status`` and ``assignees`` as the projection left them: the
    mirror's answer for an adopted item, the file's for a local one. A
    record whose status is ``None`` — an adopted item the mirror cannot
    speak for — matches no ``status`` filter, because "unknown" is not
    "open" and guessing would be the stale-state lie §5.3 forbids.
    """
    if status is not None and record.get("status") != status:
        return False
    if assignees is None:
        return True
    found = {
        value.casefold()
        for value in record.get("assignees") or []
        if isinstance(value, str) and value
    }
    single = record.get("assignee")
    if isinstance(single, str) and single:
        # The projection fills ``assignees`` for both kinds, so this is
        # belt and braces — and it is what keeps the predicate correct on a
        # record that never went through the projection at all.
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
