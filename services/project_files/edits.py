"""The version store: every project file change by any writer, kept in the private state root, never in the project."""

# Same path and writer within MERGE_WINDOW_S extends the newest record; re-recording its version is skipped.

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from services.cowork_agent.project_layout import project_runtime_dir, xo_projects_root
from services.project_files import events
from services.project_files.address import (
    EDIT_MAX_BYTES,
    RUNS_CODE_CONFIRM,
    SPACE_ID,
    VERSION_PREFIX,
    FileRefused,
    Place,
    Target,
    is_agents,
    open_file,
    parent_dir,
    read_fd,
    version_of,
    worktree_of,
)
from services.storage.atomic_write import create_file_exclusive, write_text_durable
from services.storage.layout import projects_dir
from services.timestamps import parse_ts

log = logging.getLogger(__name__)

#: Under the project's own folder of the state root (``projects/<pid>/``).
STATE_DIRNAME = "files"
LOG_NAME = "edits.jsonl"
RETENTION_DAYS = 30
MAX_BLOB_BYTES = 200 * 1024 * 1024
MAX_LOG_LINES = 20_000
PRUNE_EVERY_S = 3600.0
LIST_MAX = 200
#: Changes of one path by one writer closer than this are one version.
MERGE_WINDOW_S = 10.0

#: What a record says happened.
KINDS = ("edit", "create", "delete", "rename", "restore", "undo")
#: Who made a change.
UI, AGENT, DISK = "ui", "agent", "disk"
#: The id prefixes: ``v_`` now, ``e_`` for the person's saves logged before.
ID_PREFIX = "v_"

_pruned_at: dict[str, float] = {}
_prune_guard = threading.Lock()


def writer(kind: str, *, session_id: str | None = None, thread_ts: str | None = None,
           turn_id: str | None = None, message_id: str | None = None) -> dict[str, Any]:
    """A record's ``by``."""
    return {"kind": kind, "session_id": session_id, "thread_ts": thread_ts, "turn_id": turn_id,
            "message_id": message_id}


def _same_writer(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return a.get("kind") == b.get("kind") and (a.get("kind") != AGENT or a.get("session_id") == b.get("session_id"))


@dataclass(frozen=True)
class Edit:
    """One version record; ``content_kept`` says whether its bytes were stored, ``group`` ties one rename/delete."""

    id: str
    path: str
    real: str
    worktree: str | None
    kind: str
    from_version: str | None
    to_version: str | None
    at: str
    by: Mapping[str, Any] = field(default_factory=lambda: writer(UI))
    size_bytes: int | None = None
    content_kept: bool = True
    old_path: str | None = None
    old_real: str | None = None
    confirmed: str | None = None
    undoes: str | None = None
    group: str | None = None
    #: A ``restore``'s source: the record whose version the person put back.
    restores: str | None = None

    @property
    def when(self) -> datetime:
        return parse_ts(self.at) or datetime.fromtimestamp(0, timezone.utc)

    @property
    def by_kind(self) -> str:
        return self.by.get("kind") or UI

    @property
    def version(self) -> str | None:
        """The version the record is about: what it wrote, or what a delete removed."""
        return self.to_version if self.to_version is not None else self.from_version


def _now() -> datetime:
    """The store's clock (one place, so the merge window can be tested)."""
    return datetime.now(timezone.utc)


def now_precise() -> str:
    """UTC with milliseconds: an edit and a turn can share a second."""
    stamp = _now()
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def new_id() -> str:
    return ID_PREFIX + secrets.token_hex(8)


# ── where ─────────────────────────────────────────────────────────────────────


def state_dir(project_id: str) -> Path:
    """The project's private state folder: version store, kept versions, trash, and /changes' index.
    The Space's agents keep theirs under ``projects/.space/``: no project key can be a dot-name."""
    if project_id == SPACE_ID:
        return projects_dir() / SPACE_ID / STATE_DIRNAME
    return project_runtime_dir(project_id) / STATE_DIRNAME


def _blob_path(folder: Path, version: str) -> Path:
    return folder / "blobs" / f"{version.removeprefix(VERSION_PREFIX)}.z"


@contextlib.contextmanager
def _locked(folder: Path, *, shared: bool = False) -> Iterator[None]:
    """The log's flock: exclusive to write (record time taken under it keeps time order), shared to read."""
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(folder / "edits.lock", "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        except BaseException:
            with _logs_guard:                      # records applied but never written must not stay cached
                _logs.pop(str(folder), None)
            raise
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ── what a version is ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Content:
    """One file version: hash, size, and its bytes when held (never over ``EDIT_MAX_BYTES``)."""

    version: str
    size: int
    data: bytes | None = None


def content_of(data: bytes) -> Content:
    return Content(version_of(data), len(data), data)


def content_from_fd(fd: int) -> Content:
    """A file's version from its descriptor: bytes up to ``EDIT_MAX_BYTES``, a streamed hash beyond."""
    head = b""
    while len(head) <= EDIT_MAX_BYTES:
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            return content_of(head)
        head += chunk
    digest = hashlib.sha256(head)
    size = len(head)
    while chunk := os.read(fd, 1 << 20):
        digest.update(chunk)
        size += len(chunk)
    return Content(VERSION_PREFIX + digest.hexdigest(), size, None)


def content_at(path: Path) -> Content | None:
    """The version of the regular file at ``path``, never following its final name; None if absent or unreadable."""
    return stamped_content_at(path)[0]


def file_stamp(st: os.stat_result) -> tuple:
    """What changes when a file is replaced or written."""
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def stamped_content_at(path: Path) -> tuple[Content | None, tuple | None]:
    """``content_at`` plus the file's stamp, or None for the stamp when the file changed while read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None, None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None, None
        found = content_from_fd(fd)
        after = os.fstat(fd)
    except OSError:
        return None, None
    finally:
        os.close(fd)
    return found, file_stamp(after) if file_stamp(before) == file_stamp(after) else None


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


#: Credential-looking files (owner's choice, 5 Nov 2026): the versions a
#: record replaced or wrote are never kept on disk, only in this process's
#: memory for ``VOLATILE_S``, so an undo right after a save still works and
#: nothing of a secret outlives it in the state root.
SECRET_NAMES = (".env", ".env.*", "*.pem", "*.key", "id_*")
NOT_SECRET = ("*.example", "*.sample", "*.template", "*.dist", "*.pub")
VOLATILE_S = 15 * 60
_volatile: dict[str, tuple[bytes, float]] = {}
_volatile_guard = threading.Lock()


def is_secret_name(name: str) -> bool:
    lower = name.lower()
    return any(fnmatchcase(lower, p) for p in SECRET_NAMES) and not any(fnmatchcase(lower, p) for p in NOT_SECRET)


def is_secret_path(*rels: str | None) -> bool:
    return any(rel and is_secret_name(rel.rsplit("/", 1)[-1]) for rel in rels)


def hold(data: bytes) -> None:
    """Keep a credential file's version in memory only, for ``VOLATILE_S``."""
    now = time.monotonic()
    with _volatile_guard:
        for version in [v for v, (_d, until) in _volatile.items() if until < now]:
            del _volatile[version]
        _volatile[version_of(data)] = (data, now + VOLATILE_S)


def held(version: str) -> bytes | None:
    with _volatile_guard:
        found = _volatile.get(version)
    return found[0] if found and found[1] >= time.monotonic() else None


def _keep(folder: Path, data: bytes) -> None:
    path = _blob_path(folder, version_of(data))
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    create_file_exclusive(path, zlib.compress(data, 6))


def _store(folder: Path, content: Content | None, secret: bool) -> bool:
    """Keep one version's bytes as the rules say; whether they were kept."""
    if content is None or content.data is None or len(content.data) > EDIT_MAX_BYTES or not _is_text(content.data):
        return False
    if secret:
        hold(content.data)
    else:
        _keep(folder, content.data)
    return True


def _has(folder: Path, version: str | None, secret: bool) -> bool:
    if version is None:
        return False
    return held(version) is not None if secret else _blob_path(folder, version).exists()


# ── the log, read once and kept current ──────────────────────────────────────


#: A path's state when no record names it.
UNKNOWN = object()


class _Log:
    """A project's parsed log: records in time order, each path's newest version; valid while the file is unchanged."""

    def __init__(self, key: tuple | None) -> None:
        self.key = key
        self.records: dict[str, Edit] = {}
        self.state: dict[str, str | None] = {}
        self.newest: dict[str, str] = {}

    def apply(self, edit: Edit) -> None:
        previous = self.records.get(edit.id)
        if previous is not None and previous.at == edit.at:
            self.records[edit.id] = edit                 # attributed: in place
        else:
            self.records.pop(edit.id, None)              # new, or extended: now the newest
            self.records[edit.id] = edit
        if previous is not None and previous.at == edit.at:
            return                                       # who, not what: no path's state moves
        if edit.old_real and edit.old_real != edit.real:
            self.state[edit.old_real] = None
        self.state[edit.real] = edit.to_version
        self.newest[edit.real] = edit.id

    def path_state(self, real: str) -> Any:
        return self.state.get(real, UNKNOWN)

    def newest_of(self, real: str) -> Edit | None:
        found = self.newest.get(real)
        return self.records.get(found) if found else None


_logs: dict[str, _Log] = {}
_logs_guard = threading.Lock()


def _key(folder: Path) -> tuple | None:
    try:
        st = os.stat(folder / LOG_NAME)
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns


def _parse_line(line: str) -> Edit | None:
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("kind") == "save":                       # the person's edit log, before every writer was kept
        raw["kind"] = "edit"
    if isinstance(raw.get("by"), str) or raw.get("by") is None:
        raw["by"] = writer(raw.get("by") or UI)
    try:
        return Edit(**{k: v for k, v in raw.items() if k in _FIELDS})
    except TypeError:
        return None


_FIELDS = set(Edit.__dataclass_fields__)


def _load(folder: Path) -> _Log:
    """The folder's log, reparsed only when the file changed; call under the log's lock."""
    key = _key(folder)
    with _logs_guard:
        cached = _logs.get(str(folder))
    if cached is not None and cached.key == key:
        return cached
    parsed = _Log(key)
    if key is not None:
        try:
            text = (folder / LOG_NAME).read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        for line in text.splitlines():
            if (edit := _parse_line(line)) is not None:
                parsed.apply(edit)
    with _logs_guard:
        _logs[str(folder)] = parsed
    return parsed


# ── writing ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Change:
    """One change to record (``before``/``after`` None means absent); ``kind`` ``auto`` lets the store decide."""

    path: str
    real: str
    kind: str
    before: Content | None
    after: Content | None
    by: Mapping[str, Any]
    old_path: str | None = None
    old_real: str | None = None
    confirmed: str | None = None
    undoes: str | None = None
    group: str | None = None
    restores: str | None = None
    #: The watcher saw it appear (``create`` when nothing was known of it).
    added: bool = False
    #: Whether a merge window may extend the path's newest record.
    mergeable: bool = True
    id: str | None = None


#: What happened to a change: a new record, the newest extended, or nothing.
NEW, MERGED, SKIPPED = "new", "merged", "skipped"


def _decide(parsed: _Log, change: Change, now: str, folder: Path) -> tuple[Edit | None, str]:
    state = parsed.path_state(change.real)
    to_version = change.after.version if change.after is not None else None
    kind = change.kind
    if kind == "auto":
        if state is not UNKNOWN and state == to_version:
            return None, SKIPPED
        if to_version is None:
            if state is UNKNOWN or state is None:
                return None, SKIPPED                     # gone, and nothing was known of it
            kind = "delete"
        else:
            kind = "create" if state is None or (state is UNKNOWN and change.added) else "edit"
        before_version = state if isinstance(state, str) else None
    else:
        # The caller read what it replaced: trust that, not what the store last
        # knew (the watcher may have missed a change while it was off).
        before_version = change.before.version if change.before is not None else None
        if change.old_real is None and before_version == to_version:
            return None, SKIPPED                         # nothing changed (a move changes its path)
    secret = is_secret_path(change.real, change.path)
    kept_before = _store(folder, change.before, secret)
    if change.after is not None:
        kept = _store(folder, change.after, secret)
    else:                                                # a delete: what it removed
        kept = kept_before or _has(folder, before_version, secret)
    size = change.after.size if change.after is not None else None

    newest = parsed.newest_of(change.real)
    if (change.mergeable and kind == "edit" and newest is not None and newest.kind in ("edit", "create")
            and newest.group is None and newest.undoes is None and newest.to_version == before_version
            and _same_writer(newest.by, change.by)
            and (_now() - newest.when).total_seconds() < MERGE_WINDOW_S):
        by = dict(change.by)
        if by.get("turn_id") and by.get("turn_id") == newest.by.get("turn_id") and not by.get("message_id"):
            by["message_id"] = newest.by.get("message_id")
        return replace(newest, to_version=to_version, at=now, size_bytes=size, content_kept=kept, by=by,
                       confirmed=change.confirmed or newest.confirmed), MERGED
    return Edit(
        id=change.id or new_id(),
        path=change.path,
        real=change.real,
        worktree=worktree_of(change.path),
        kind=kind,
        from_version=before_version,
        to_version=to_version,
        at=now,
        by=dict(change.by),
        size_bytes=size,
        content_kept=kept,
        old_path=change.old_path,
        old_real=change.old_real,
        confirmed=change.confirmed,
        undoes=change.undoes,
        group=change.group,
        restores=change.restores,
    ), NEW


def write(project_id: str, changes: Iterable[Change]) -> list[tuple[Edit | None, str]]:
    """Record changes in order under the lock, with each outcome; published after unlocking; OSError if unwritable."""
    folder = state_dir(project_id)
    out: list[tuple[Edit | None, str]] = []
    with _locked(folder):
        parsed = _load(folder)
        written: list[Edit] = []
        for change in changes:
            edit, outcome = _decide(parsed, change, now_precise(), folder)
            out.append((edit, outcome))
            if edit is not None:
                written.append(edit)
                parsed.apply(edit)                     # the next change of this batch sees it
        _append_lines(folder, parsed, written)
    _publish_version(project_id, [e for e, outcome in out if e is not None and outcome != SKIPPED])
    _maybe_prune(project_id, folder)
    return out


def _append_lines(folder: Path, parsed: _Log, written: list[Edit]) -> None:
    """Write records already applied to ``parsed`` (one fsync)."""
    if not written:
        return
    data = "".join(json.dumps(asdict(e), separators=(",", ":")) + "\n" for e in written).encode("utf-8")
    fd = os.open(folder / LOG_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        start = os.fstat(fd).st_size
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except BaseException:
            with contextlib.suppress(OSError):     # a half-written line would spoil the record after it
                os.ftruncate(fd, start)
            raise
    finally:
        os.close(fd)
    parsed.key = _key(folder)


def record(target: Target, before: bytes | None, after: bytes | None, *, kind: str,
           confirmed: str | None = None, undoes: str | None = None, restores: str | None = None) -> Edit | None:
    """Record one of the person's Files changes; None when nothing new; OSError if the log can't be written."""
    ((edit, _outcome),) = write(target.project_id, [Change(
        path=target.rel, real=target.real_rel, kind=kind,
        before=content_of(before) if before is not None else None,
        after=content_of(after) if after is not None else None,
        by=writer(UI), confirmed=confirmed, undoes=undoes, restores=restores,
        mergeable=undoes is None and restores is None)])
    return edit


@dataclass(frozen=True)
class Seen:
    """What the watcher found at one path: bytes now (None: gone), presumed writer, appeared, and matches git."""

    real: str
    after: Content | None
    by: Mapping[str, Any]
    added: bool = False
    in_git: bool = False


def observe(project_id: str, seen: Iterable[Seen]) -> list[Edit]:
    """Record a watcher batch as edits, deletes or renames (gone + appeared at the same version); returns records."""
    seen = list(seen)
    folder = state_dir(project_id)
    with _locked(folder):
        parsed = _load(folder)
        gone = {s.real: parsed.path_state(s.real) for s in seen if s.after is None}
        gone = {real: v for real, v in gone.items() if isinstance(v, str)}
        by_version: dict[str, list[str]] = {}
        for real, version in gone.items():
            by_version.setdefault(version, []).append(real)
        changes: list[Change] = []
        renamed: set[str] = set()
        for s in seen:
            if s.after is None:
                continue
            state = parsed.path_state(s.real)
            sources = by_version.get(s.after.version)
            if s.in_git and state is UNKNOWN and not sources:
                continue                     # put there by git (a checkout): no one's new version
            if sources and (state is None or state is UNKNOWN):
                source = sources.pop(0)
                renamed.add(source)
                changes.append(Change(path=s.real, real=s.real, kind="rename", before=s.after, after=s.after,
                                      by=s.by, old_path=source, old_real=source, mergeable=False))
            else:
                changes.append(Change(path=s.real, real=s.real, kind="auto", before=None, after=s.after, by=s.by,
                                      added=s.added))
        changes += [Change(path=s.real, real=s.real, kind="auto", before=None, after=None, by=s.by)
                    for s in seen if s.after is None and s.real not in renamed]
        written: list[Edit] = []
        for change in changes:
            edit, outcome = _decide(parsed, change, now_precise(), folder)
            if edit is not None:
                parsed.apply(edit)
                written.append(edit)
        _append_lines(folder, parsed, written)
    _publish_version(project_id, written)
    _maybe_prune(project_id, folder)
    return written


def live_under(project_id: str, prefix: str) -> list[str]:
    """The resolved paths the store knows exist under folder ``prefix``."""
    folder = state_dir(project_id)
    if not (folder / LOG_NAME).exists():
        return []
    with _locked(folder, shared=True):
        parsed = _load(folder)
        start = prefix.rstrip("/") + "/"
        return [real for real, version in parsed.state.items() if version is not None and real.startswith(start)]


def attribute(project_id: str, real: str, *, since: datetime, until: datetime | None = None,
              by: Mapping[str, Any], current: Content | None, created: bool = False) -> tuple[Edit | None, str]:
    """At a turn's end, give the agent the newest record in its window, or record ``current`` as the agent's."""
    folder = state_dir(project_id)
    since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    with _locked(folder):
        parsed = _load(folder)
        mine = [e for e in parsed.records.values() if e.real == real]
        # The person's own saves are theirs for certain: never handed to an agent.
        found = next((e for e in reversed(mine) if e.by_kind != UI and e.when >= since
                      and (until is None or e.when <= until)), None)
        if found is None and until is not None and any(e.when > until for e in mine):
            return None, "unchanged"                     # what is there now is a later writer's
        if found is not None:
            merged_by = {**writer(AGENT), **{k: v for k, v in by.items() if v is not None}}
            if dict(found.by) == merged_by:
                return found, "unchanged"
            edit = replace(found, by=merged_by)
            parsed.apply(edit)
            _append_lines(folder, parsed, [edit])
            result: tuple[Edit | None, str] = (edit, "attributed")
        else:
            if current is None:
                return None, "unchanged"
            edit, outcome = _decide(parsed, Change(path=real, real=real, kind="auto", before=None, after=current,
                                                   by={**writer(AGENT), **by}, added=created, mergeable=False),
                                    now_precise(), folder)
            if edit is None:
                return None, "unchanged"
            parsed.apply(edit)
            _append_lines(folder, parsed, [edit])
            result = (edit, NEW)
    if result[1] == NEW:
        _publish_version(project_id, [result[0]])
    else:
        events.publish(events.ATTRIBUTED, project_id, {"project_id": project_id, "path": result[0].path,
                                                       "id": result[0].id, "by": dict(result[0].by)})
    return result


# ── reading ───────────────────────────────────────────────────────────────────


def entries(project_id: str) -> list[Edit]:
    """Every record, oldest first; a line that doesn't parse is skipped."""
    return _entries(state_dir(project_id))


def _entries(folder: Path) -> list[Edit]:
    if not (folder / LOG_NAME).exists():
        return []
    try:
        with _locked(folder, shared=True):
            return list(_load(folder).records.values())
    except FileNotFoundError:
        return []


def find(project_id: str, edit_id: str) -> Edit | None:
    folder = state_dir(project_id)
    if not (folder / LOG_NAME).exists():
        return None
    with _locked(folder, shared=True):
        return _load(folder).records.get(edit_id)


def kept_version(project_id: str, version: str) -> bytes | None:
    """A version the store kept, or None when pruned or never kept."""
    if (found := held(version)) is not None:
        return found
    try:
        data = zlib.decompress(_blob_path(state_dir(project_id), version).read_bytes())
    except (FileNotFoundError, zlib.error):
        return None
    return data if version_of(data) == version else None


def is_kept(project_id: str, edit: Edit) -> bool:
    """Whether the version a record is about can still be read (kept and not pruned or forgotten)."""
    version = edit.version
    if not edit.content_kept or version is None:
        return False
    if is_secret_path(edit.real, edit.path):
        return held(version) is not None
    return _blob_path(state_dir(project_id), version).exists()


def current_version(file: Path) -> str | None:
    """The version on disk now, read without following a final link; None
    when there is no regular file (or it is too large to have one)."""
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > EDIT_MAX_BYTES:
            return None
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return version_of(b"".join(chunks))
    finally:
        os.close(fd)



def legacy_view(edit: Edit) -> dict[str, Any]:
    """A record in the legacy ``GET /edits`` shape (``kind`` ``save`` for edits, ``by: "ui"``, ``via: "files"``)."""
    return {"id": edit.id, "path": edit.path, "real": edit.real, "worktree": edit.worktree,
            "kind": "save" if edit.kind == "edit" else edit.kind, "from_version": edit.from_version,
            "to_version": edit.to_version, "at": edit.at, "confirmed": edit.confirmed, "undoes": edit.undoes,
            "size_bytes": edit.size_bytes, "by": UI, "via": "files", "old_path": edit.old_path,
            "group": edit.group}


def listing(project_id: str, root: Path, *, path: str | None = None, worktree: str | None = None,
            since: datetime | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """The person's records newest first for ``GET /edits``, each with ``still_current`` and ``undoable``."""
    folder = state_dir(project_id)
    found = [e for e in reversed(_entries(folder))
             if e.by_kind == UI
             and (path is None or e.path == path or e.real == path)
             and (worktree is None or e.worktree == worktree)
             and (since is None or e.when > since)][: max(1, min(limit, LIST_MAX))]
    current: dict[str, str | None] = {}
    out = []
    for edit in found:
        if edit.real not in current:
            current[edit.real] = current_version(root / edit.real)
        still = current[edit.real] == edit.to_version
        if edit.group is not None:                       # a rename or delete: ops undoes it whole
            restorable = _op_kind(folder, edit.group) in ("rename", "delete")
        else:
            restorable = edit.from_version is None or _blob_path(folder, edit.from_version).exists() \
                or held(edit.from_version) is not None
        out.append({**legacy_view(edit), "still_current": still, "undoable": still and restorable})
    return out


def _op_kind(folder: Path, group: str) -> str | None:
    """The kind of the operation a record belongs to (``ops``' journal)."""
    if "/" in group or group.startswith("."):
        return None
    try:
        return json.loads((folder / OPS_DIRNAME / f"{group}.json").read_text(encoding="utf-8")).get("kind")
    except (OSError, ValueError, AttributeError):
        return None


def history(project_id: str, real: str | None = None, *, since: datetime | None = None) -> list[Edit]:
    """Records newest first for one resolved path (a rename's is its target's), or the whole project, since a time."""
    folder = state_dir(project_id)
    items = _entries(folder)
    return [e for e in reversed(items)
            if (real is None or e.real == real)
            and (since is None or e.when >= since)]


# ── what an agent engine reads ────────────────────────────────────────────────


def _project_of(path: Path) -> tuple[str, Path] | None:
    """The project a resolved path is in: ``(project id, project root)``."""
    root = xo_projects_root().resolve()
    if not path.is_relative_to(root) or path == root:
        return None
    pid = path.relative_to(root).parts[0]
    return pid, root / pid


def project_of(path: Path) -> tuple[str, Path] | None:
    """``(project id, project root)`` of a path (resolved), if it is in one."""
    return _project_of(Path(os.path.realpath(path)))


def edits_since(folder: Path, since: datetime) -> list[dict[str, Any]]:
    """The person's Files edits per file in ``folder`` after ``since``, minus inner worktrees and ``.xo/``
    (except the project's agents, ``.xo/agents/``)."""
    folder = Path(os.path.realpath(folder))
    found = _project_of(folder)
    if found is None:
        return []
    pid, root = found
    try:
        logged = entries(pid)
    except OSError:
        return []
    since = since if since.tzinfo else since.replace(tzinfo=timezone.utc)

    def inside(real: str | None) -> str | None:
        if not real:
            return None
        file = root / real
        if not file.is_relative_to(folder):
            return None
        rel = file.relative_to(folder).as_posix()
        return None if rel.split("/", 1)[0] == ".xo" and not is_agents(rel) else rel

    by_path: dict[str, dict[str, Any]] = {}
    for edit in logged:
        if edit.when <= since or edit.by_kind != UI:
            continue
        rel = inside(edit.real)
        if rel is None:
            continue
        entry = by_path.setdefault(rel, {"path": rel, "edits": 0})
        entry.update(edits=entry["edits"] + 1, last_at=edit.at, kind=edit.kind,
                     exists=edit.to_version is not None)
        if edit.old_real and edit.old_real != edit.real:
            if (source := inside(edit.old_real)) is not None:
                entry["from"] = source
                by_path.pop(source, None)
    return sorted(by_path.values(), key=lambda e: e["last_at"])


def written_by_person(file: Path) -> bool:
    """Whether ``file`` holds exactly what the person last saved there with confirmation; never resolves links."""
    file = Path(os.path.normpath(file))
    root = xo_projects_root().resolve()
    if not file.is_absolute() or not file.is_relative_to(root):
        return False
    parts = file.relative_to(root).parts
    if len(parts) < 2:
        return False
    pid, rel = parts[0], "/".join(parts[1:])
    try:
        latest = next((e for e in reversed(entries(pid)) if e.real == rel or e.path == rel), None)
    except OSError:
        return False
    if latest is None or latest.path != rel or latest.real != rel or latest.to_version is None \
            or latest.confirmed != RUNS_CODE_CONFIRM or latest.by_kind != UI:
        return False
    target = Target(pid, root / pid, rel, rel, Place.RUNS_CODE)
    try:
        with parent_dir(target) as folder:
            fd = open_file(folder, rel.rsplit("/", 1)[-1])
            if fd is None:
                return False
            try:
                data = read_fd(fd, EDIT_MAX_BYTES + 1)
            finally:
                os.close(fd)
    except (FileRefused, OSError):
        return False
    return len(data) <= EDIT_MAX_BYTES and version_of(data) == latest.to_version


# ── what goes out ─────────────────────────────────────────────────────────────


def version_payload(project_id: str, edit: Edit) -> dict[str, Any]:
    """A ``file.version`` event's data (contract §3)."""
    return {"project_id": project_id, "path": edit.path, "worktree": edit.worktree, "id": edit.id,
            "kind": edit.kind, "old_path": edit.old_path, "version": edit.to_version,
            "prev_version": edit.from_version, "by": dict(edit.by), "at": edit.at, "size_bytes": edit.size_bytes,
            "content_kept": is_kept(project_id, edit), "restores": edit.restores}


def _publish_version(project_id: str, written: list[Edit]) -> None:
    for edit in written:
        events.publish(events.VERSION, project_id, version_payload(project_id, edit))


# ── retention ─────────────────────────────────────────────────────────────────


def _maybe_prune(project_id: str, folder: Path) -> None:
    with _prune_guard:
        last = _pruned_at.get(project_id, 0.0)
        if time.monotonic() - last < PRUNE_EVERY_S and last:
            return
        _pruned_at[project_id] = time.monotonic()
    try:
        prune(folder)
    except OSError:
        log.warning("project files: couldn't prune the version store of %s", project_id, exc_info=True)


#: The operations journal and the trash (``services.project_files.ops``).
OPS_DIRNAME = "ops"
TRASH_DIRNAME = "trash"


def prune(folder: Path, *, now: datetime | None = None) -> None:
    """Apply retention: drop old records and compact the log, prune blobs to ``MAX_BLOB_BYTES``, old ops and trash."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    with _locked(folder):
        parsed = _load(folder)
        logged = list(parsed.records.values())
        kept = [e for e in logged if e.when > cutoff][-MAX_LOG_LINES:]
        if (folder / LOG_NAME).exists() and (len(kept) != len(logged) or _lines(folder) != len(kept)):
            write_text_durable(folder / LOG_NAME,
                               "".join(json.dumps(asdict(e), separators=(",", ":")) + "\n" for e in kept))
        _prune_ops(folder, cutoff)
        # Newest reference first: what goes when space runs out is the oldest.
        order = list(dict.fromkeys(version for edit in reversed(kept)
                                   for version in (edit.to_version, edit.from_version) if version))
        blobs = folder / "blobs"
        if not blobs.is_dir():
            return
        wanted = {v.removeprefix(VERSION_PREFIX) for v in order}
        sizes: dict[str, int] = {}
        for entry in os.scandir(blobs):
            name = entry.name.removesuffix(".z")
            if name not in wanted:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(entry.path)
            else:
                sizes[name] = entry.stat().st_size
        total = 0
        for version in order:
            name = version.removeprefix(VERSION_PREFIX)
            if name not in sizes:
                continue
            total += sizes[name]
            if total > MAX_BLOB_BYTES:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(blobs / f"{name}.z")


def _lines(folder: Path) -> int:
    try:
        with open(folder / LOG_NAME, "rb") as handle:
            return sum(1 for _ in handle)
    except FileNotFoundError:
        return 0


def _prune_ops(folder: Path, cutoff: datetime) -> None:
    ops = folder / OPS_DIRNAME
    if not ops.is_dir():
        return
    for entry in os.scandir(ops):
        if not entry.name.endswith(".json"):
            continue
        try:
            at = parse_ts(json.loads(Path(entry.path).read_text(encoding="utf-8")).get("at") or "")
        except (OSError, ValueError):
            at = None
        if at is not None and at > cutoff:
            continue
        group = entry.name.removesuffix(".json")
        shutil.rmtree(folder / TRASH_DIRNAME / group, ignore_errors=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(entry.path)
