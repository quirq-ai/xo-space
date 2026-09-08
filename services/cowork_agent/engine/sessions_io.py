"""
Session-file I/O, and the per-project session index itself.

Concerns:
- the partitioned session index: where its shards live, how they merge, how
  one row is written (see "The session index" below)
- listing sessions across backends and sorting by updated time
- finding the message file for a given session id
- persisting a user-selected `directory` into the matching index row

The session index
-----------------
The index is the map ``{<composite key>: <adapter row>}`` every project-tied
backend publishes a row into, and that the visualizer, the sidebar and the
ownership routes all read. Two properties of it changed in syncplan T19:

**Where it lives.** It is machine-local derived state, so it moved out of the
synced project tree and into ``~/.quirq/projects/<key>/sessions/`` with the
rest of the runtime tier (§4, R-TIER). Nothing here builds that path: it comes
from ``project_layout``, which applies the folder resolution itself — no
adapter in this tree imported ``resolve_project_dirname``, so a helper that
trusted its callers would have re-opened the same hole.

**How it is written.** It used to be one file with 15 unlocked
read-modify-write sites across 9 modules, so two concurrent writers holding
two *different* rows lost one of them — T4 closed the shared-temp collision and
the non-atomic writes, but not that. R-CONTEND says partition before you lock,
so each row is now its own shard file under ``sessionslist.d/`` and a writer
only ever replaces its own row. Readers merge the directory. There is no
shared document left for two writers to race over, which is why this needs no
lock and no bounded wait.

Reads fall through to the pre-move ``<project>/.xo/sessions/sessionslist.json``
(and the older ``sessions.json``) so a project that predates the move keeps
serving its rows; the first write of a row lands in the runtime tier and from
then on that shard is what answers. That is the same read-through +
copy-on-first-write migration the ``~/.xo-cowork`` → ``~/.quirq`` move used,
with no startup mover to race the watcher.

Security model
--------------
The index holds only metadata. Chat messages are never written to it. They
live in each backend's own storage, reached only through that backend's
``sessions`` capability (``resolve_native_file`` / ``get_messages``); core
never reads a backend's message store directly and names no backend here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Iterator, Optional

from services.cowork_agent import project_layout
from services.cowork_agent.helpers import iso_now, ms_to_iso
from services.cowork_agent.project_layout import xo_projects_root

logger = logging.getLogger(__name__)


# ── The partitioned session index ─────────────────────────────────────────────

#: Whole-file index names, newest first. Only ever READ now — they are the
#: pre-T19 shape, kept so an existing project's rows survive the move.
_LEGACY_INDEX_FILENAMES = ("sessionslist.json", "sessions.json")


def _resolve_index_path(sessions_dir: Path) -> Path | None:
    """Return the first existing whole-file index, preferring the new name.

    Legacy read path only: nothing writes a whole-file index any more.
    """
    for fname in _LEGACY_INDEX_FILENAMES:
        p = sessions_dir / fname
        if p.exists():
            return p
    return None


def shard_filename(composite_key: str) -> str:
    """File name of the shard that owns ``composite_key``.

    A digest, not the key itself: composite keys carry ``:`` separators and
    arbitrary agent ids, neither of which is safe to use as a filename. The
    key is stored *inside* the shard, so the merge never has to decode it.
    """
    digest = hashlib.sha256(composite_key.encode("utf-8")).hexdigest()[:16]
    return f"{digest}.json"


def _write_shard_atomic(path: Path, document: dict) -> None:
    """Serialize one shard to a UNIQUE temp, then ``replace`` it into place.

    Not ``write_json_atomic``: that names its temp ``<path>.tmp``, which is
    shared by every writer of that path. Partitioning makes a collision rare —
    two writers only meet on one shard when they publish the SAME composite
    key at the same moment, i.e. one session resumed twice — but rare is not
    never, and the failure is the one T4 fixed: two writers serialize into one
    another's buffer and then race to rename it, so one commits the other's
    bytes and the other's ``replace`` hits ``ENOENT``.

    ``<name>.tmp.<pid>.<8hex>`` cannot collide. A leaked unique temp
    accumulates where a leaked shared one does not, so a failed write unlinks
    its own.
    """
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_index_document(path: Path) -> dict:
    """One ``{key: row}`` document, or ``{}`` if unreadable.

    Logged at debug, not swallowed silently: a shard that stops parsing costs
    exactly one row, which is the improvement over the single document (where
    it cost every row), but it should still be findable.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("session index: skipping %s: %s", path, exc)
        return {}
    if not isinstance(doc, dict):
        logger.debug("session index: %s is not a JSON object", path)
        return {}
    return {k: v for k, v in doc.items() if isinstance(v, dict)}


def _read_whole_file_index(sessions_dir: Optional[Path]) -> dict:
    if sessions_dir is None:
        return {}
    path = _resolve_index_path(sessions_dir)
    if path is None:
        return {}
    return _read_index_document(path)


def _read_shards(shard_dir: Optional[Path]) -> dict:
    """Merge every shard in ``shard_dir``. Order: sorted by file name.

    A shard that is missing, unreadable or malformed is skipped rather than
    failing the merge — one corrupt row must not blank the whole index, which
    is precisely the failure the single-file shape had.
    """
    merged: dict = {}
    if shard_dir is None:
        return merged
    try:
        entries = sorted(shard_dir.iterdir())
    except OSError:
        return merged
    for entry in entries:
        if entry.suffix != ".json":
            continue
        merged.update(_read_index_document(entry))
    return merged


def read_session_index_at(
    runtime_root: Optional[Path], *, legacy_root: Optional[Path] = None
) -> dict:
    """Merged ``{key: row}`` for one project, given its roots.

    ``runtime_root`` is ``~/.quirq/projects/<key>/``; ``legacy_root`` is the
    project's ``.xo/``, read only so pre-move rows still surface. Precedence,
    lowest to highest: legacy whole file, a whole file sitting in the runtime
    sessions dir (hand-copied; nothing writes one), then the shards.
    """
    merged: dict = {}
    if legacy_root is not None:
        merged.update(
            _read_whole_file_index(legacy_root / project_layout.LEGACY_SESSIONS_SUBDIR)
        )
    if runtime_root is not None:
        merged.update(
            _read_whole_file_index(
                runtime_root / project_layout.RUNTIME_SESSIONS_SUBDIR
            )
        )
        merged.update(
            _read_shards(runtime_root / project_layout.RUNTIME_SESSION_SHARDS_SUBDIR)
        )
    return merged


def read_session_index(name: str) -> dict:
    """Merged ``{key: row}`` for a project, resolved from its folder name.

    Empty when the project does not exist or has never had a row written —
    both are "no sessions", never an error.
    """
    runtime_root, legacy_root = project_layout.runtime_read_roots(name)
    if runtime_root is None:
        return {}
    return read_session_index_at(runtime_root, legacy_root=legacy_root)


def write_session_row(name: str, composite_key: str, row: dict) -> bool:
    """Replace one row in a project's session index. Returns ``True`` on write.

    The write touches exactly one shard file — the one this key owns — so it
    cannot lose a row another writer is publishing concurrently, whatever
    backend that writer is or which process it runs in.

    Returns ``False`` (a skip, never an exception) when the project folder
    does not exist: runtime state for a project that isn't there is state
    nothing can ever read, and creating it is how a ghost project used to
    appear in the UI.
    """
    if not name or not composite_key or not isinstance(row, dict):
        return False
    runtime_root = project_layout.runtime_dir_for_project(name, create=True)
    if runtime_root is None:
        return False
    shard_dir = runtime_root / project_layout.RUNTIME_SESSION_SHARDS_SUBDIR
    shard_dir.mkdir(parents=True, exist_ok=True)
    _write_shard_atomic(shard_dir / shard_filename(composite_key), {composite_key: row})
    return True


def iter_project_session_indexes() -> Iterator[tuple[str, Path, dict]]:
    """Yield ``(project_id, project_dir, merged_index)`` for every project.

    The replacement for the ``for entry in xo_projects_root().iterdir()`` +
    hand-built index path loop that five modules each carried their own copy
    of. Projects with no rows are skipped.
    """
    root = xo_projects_root()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        index = read_session_index(entry.name)
        if index:
            yield entry.name, entry, index


# ── Adapter sessions-capability resolution ────────────────────────────────────


def _sessions_capability(agent: str):
    """Load an adapter's ``sessions`` capability module, or None.

    The capability exposes a uniform surface across every backend — the
    listing hooks (``USES_PROJECT_SESSIONS`` / ``enrich_project_session`` /
    ``resolve_native_file`` / ``list_native_sessions``) plus the read hooks
    (``owns_session`` / ``get_messages`` / ``set_session_directory``). Core
    forwards through here instead of branching on the backend name.
    """
    if not agent:
        return None
    from services.cowork_agent.adapters.loader import try_load_capability
    return try_load_capability("sessions", agent=agent)


# ── Session listing ───────────────────────────────────────────────────────────


def load_all_sessions() -> list[dict]:
    """Scan sessions and build SessionResponse objects, filtered by active backend.

    Two scan roots are considered, both resolved through the active backend's
    ``sessions`` capability (never by naming a backend here):
    - the per-project session index (runtime tier) — project-tied sessions,
      scanned only when the active backend sets ``USES_PROJECT_SESSIONS``.
    - the backend's own native store — supplied by
      ``list_native_sessions()`` (e.g. a per-agent on-disk dir or a state db).

    Only sessions belonging to the active backend (``AGENT_NAME`` env) are
    returned: the other backends' stores aren't touched at all, so their
    sessions never leak into the sidebar. ``AGENT_NAME`` decides which world
    we're in; the other backends stay invisible.

    De-duplicated via ``sessionId`` so a session that is both project-tee'd
    and natively present surfaces only once (project-tied wins).
    """
    from services.xo_manifest import resolve_agent_name
    active_backend = resolve_agent_name()

    sessions = []
    seen_ids: set[str] = set()

    def _ingest_project_index(index_data: dict, agent_name: str, project_dir: Path) -> None:
        for key, meta in index_data.items():
            session_id = meta.get("sessionId", "")
            if not session_id or session_id in seen_ids:
                continue
            seen_ids.add(session_id)

            updated_at = meta.get("updatedAt")
            time_updated = ms_to_iso(updated_at) if updated_at else iso_now()
            time_created = time_updated
            title = "Untitled Session"

            directory = meta.get("directory", "")

            # Enrich title / time_created / effective_agent from the session's
            # OWN backend (the tag in the index), via its sessions capability —
            # no backend is named here.
            backend = meta.get("backend", "")
            bmod = _sessions_capability(backend)
            enrich = getattr(bmod, "enrich_project_session", None) if bmod else None
            if enrich:
                tc, tt, effective_agent = enrich(meta, key, agent_name)
                if tc:
                    time_created = tc
                if tt:
                    title = tt
            else:
                effective_agent = agent_name

            sessions.append({
                "id": session_id,
                "project_id": None,
                "parent_id": None,
                "slug": None,
                "agent": effective_agent,
                "directory": directory or str(project_dir),
                "title": title,
                "version": 1,
                "summary_additions": 0,
                "summary_deletions": 0,
                "summary_files": 0,
                "summary_diffs": [],
                "is_pinned": False,
                "permission": {},
                "time_created": time_created,
                "time_updated": time_updated,
                "time_compacting": None,
                "time_archived": None,
            })

    # Resolve the ACTIVE backend's sessions capability once. It decides whether
    # the xo-projects scan applies and supplies any native (non-project)
    # sessions — no backend is named here.
    active_mod = _sessions_capability(active_backend)

    # Project-tied scan: only when the active backend tees into xo-projects.
    # The per-session enrichment inside still routes by each row's OWN backend
    # tag, so a project dir holding mixed-backend sessions resolves correctly.
    if getattr(active_mod, "USES_PROJECT_SESSIONS", False):
        for project_id, project_dir, index in iter_project_session_indexes():
            _ingest_project_index(index, project_id, project_dir)

    # Native (non-project) sessions from the active backend's own store
    # (a per-agent on-disk dir, a state db, etc.); backends without one return
    # an empty list. De-duplicated by id against the project-tied rows so the
    # other backends stay invisible when they aren't active.
    lister = getattr(active_mod, "list_native_sessions", None) if active_mod else None
    if lister:
        for row in lister():
            sid = row.get("id")
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            sessions.append(row)

    sessions.sort(key=lambda s: s["time_updated"], reverse=True)
    return sessions


# ── Message file lookup ───────────────────────────────────────────────────────


def find_session_file(session_id: str) -> Path | None:
    """Find the JSONL messages file for a session.

    Resolves the native message file through the owning backend's ``sessions``
    capability (``resolve_native_file``): a project-tied session is matched by
    its sessionslist.json metadata, then handed to its backend; a non-project
    session is resolved by id against each backend's native store.
    """
    # xo-projects: check the session index for metadata to find the native file.
    for _project_id, _project_dir, index in iter_project_session_indexes():
        for meta in index.values():
            if meta.get("sessionId") != session_id:
                continue
            # Resolve the native message file via the session's OWN backend
            # capability. No backend is named here.
            bmod = _sessions_capability(meta.get("backend", ""))
            resolver = getattr(bmod, "resolve_native_file", None) if bmod else None
            if resolver:
                path = resolver(meta, session_id)
                if path:
                    return path

    # Native (non-project) sessions: ask each adapter to resolve the file by id
    # alone (used when no project was selected at chat time) — generic, each
    # backend resolves from its own native store or returns None.
    from services.cowork_agent.registry.adapter_registry import list_adapters

    for name in list_adapters():
        bmod = _sessions_capability(name)
        resolver = getattr(bmod, "resolve_native_file", None) if bmod else None
        if resolver:
            path = resolver({}, session_id)
            if path:
                return path

    return None


def find_session_backend(session_id: str) -> str | None:
    """Return the adapter name that owns session_id, or None."""
    # xo-projects: read the backend tag directly from the session index.
    for _project_id, _project_dir, index in iter_project_session_indexes():
        for meta in index.values():
            if meta.get("sessionId") == session_id:
                tag = meta.get("backend", "")
                if tag:
                    return tag

    # Not project-tagged: ask each adapter whether it owns this session via
    # its sessions capability (each backend scans its own native store).
    # No backend is named here.
    from services.cowork_agent.registry.adapter_registry import list_adapters
    from services.cowork_agent.adapters.loader import try_load_capability

    for name in list_adapters():
        mod = try_load_capability("sessions", agent=name)
        owns = getattr(mod, "owns_session", None) if mod else None
        if owns is not None and owns(session_id):
            return name

    return None
