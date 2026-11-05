"""The edit log: every save the person makes in Files, and the versions it
replaced, so a save can be undone after a reload, and an agent engine can
tell its agents what the person changed.

Kept in the project's folder of the state root
(``<state root>/projects/<pid>/files/``), never in the project: not in ``.xo/`` (the integrity watch would report
Space's own writes), out of agents' reach (the state root is private to XO),
and out of the project's git and backups.

* ``edits.jsonl``: one line per edit, appended under a lock: ``id``, the
  ``path`` as asked and the ``real`` one (links resolved), the ``worktree``
  it is in, ``kind`` (``save`` | ``create`` | ``undo``), ``from_version`` and
  ``to_version`` (``None`` for a file that wasn't there / isn't any more),
  ``at``, ``confirmed`` (``runs_code`` when the person confirmed a file that
  runs code), ``undoes`` (an undo's edit), ``size_bytes``, and who (``by:
  "ui"``, ``via: "files"``; the person's identity is a separate decision).
* ``blobs/<sha256>.z``: each version an edit replaced or wrote, compressed,
  by its hash, written once; never a credential-looking file's
  (``SECRET_NAMES``: ``.env*``, ``*.pem``, ``*.key``, ``id_*``), whose
  versions stay in memory for ``VOLATILE_S`` only.

**Retention.** ``RETENTION_DAYS`` and ``MAX_BLOB_BYTES`` per project, oldest
first, checked at most once an hour per project. An edit whose earlier
version was pruned can't be undone (``undoable: false``); the log itself is
cut at ``MAX_LOG_LINES``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import secrets
import stat
import threading
import time
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterator

from services.cowork_agent.project_layout import project_runtime_dir, xo_projects_root
from services.project_files.address import (
    EDIT_MAX_BYTES,
    RUNS_CODE_CONFIRM,
    VERSION_PREFIX,
    FileRefused,
    Place,
    Target,
    open_file,
    parent_dir,
    read_fd,
    version_of,
)
from services.storage.atomic_write import create_file_exclusive
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

_pruned_at: dict[str, float] = {}
_prune_guard = threading.Lock()


@dataclass(frozen=True)
class Edit:
    id: str
    path: str
    real: str
    worktree: str | None
    kind: str
    from_version: str | None
    to_version: str | None
    at: str
    confirmed: str | None = None
    undoes: str | None = None
    size_bytes: int | None = None
    by: str = "ui"
    via: str = "files"

    @property
    def when(self) -> datetime:
        return parse_ts(self.at) or datetime.fromtimestamp(0, timezone.utc)


def now_precise() -> str:
    """UTC with milliseconds: an edit and a turn can share a second."""
    stamp = datetime.now(timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


# ── where ─────────────────────────────────────────────────────────────────────


def state_dir(project_id: str) -> Path:
    """``~/.quirq/projects/<pid>/files/``: the edit log, the kept versions,
    and ``/changes``' private index."""
    return project_runtime_dir(project_id) / STATE_DIRNAME


def _blob_path(folder: Path, version: str) -> Path:
    return folder / "blobs" / f"{version.removeprefix(VERSION_PREFIX)}.z"


@contextlib.contextmanager
def _locked(folder: Path, *, shared: bool = False) -> Iterator[None]:
    """The log's lock: exclusive to write (an edit's time is taken under it,
    so the log is in time order), shared to read (a reader sees every edit
    timed before it took the lock)."""
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(folder / "edits.lock", "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ── writing ───────────────────────────────────────────────────────────────────


def record(target: Target, before: bytes | None, after: bytes | None, *, kind: str,
           confirmed: str | None = None, undoes: str | None = None) -> Edit:
    """Log one edit and keep both versions. Raises ``OSError`` when the log
    can't be written (the caller has already saved the file and says so)."""
    folder = state_dir(target.project_id)
    secret = is_secret_name(target.real_rel.rsplit("/", 1)[-1]) or is_secret_name(target.name)
    with _locked(folder):
        for data in (before, after):
            if data is None:
                continue
            if secret:
                _hold(data)
            else:
                _keep(folder, data)
        edit = Edit(
            id="e_" + secrets.token_hex(8),
            path=target.rel,
            real=target.real_rel,
            worktree=target.worktree,
            kind=kind,
            from_version=version_of(before) if before is not None else None,
            to_version=version_of(after) if after is not None else None,
            at=now_precise(),
            confirmed=confirmed,
            undoes=undoes,
            size_bytes=len(after) if after is not None else None,
        )
        with open(folder / LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(edit), separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    _maybe_prune(target.project_id, folder)
    return edit


#: Credential-looking files (owner's choice, 5 Nov 2026): the versions an
#: edit replaced or wrote are never kept on disk, only in this process's
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


def _hold(data: bytes) -> None:
    now = time.monotonic()
    with _volatile_guard:
        for version in [v for v, (_d, until) in _volatile.items() if until < now]:
            del _volatile[version]
        _volatile[version_of(data)] = (data, now + VOLATILE_S)


def _held(version: str) -> bytes | None:
    with _volatile_guard:
        found = _volatile.get(version)
    return found[0] if found and found[1] >= time.monotonic() else None


def _keep(folder: Path, data: bytes) -> None:
    path = _blob_path(folder, version_of(data))
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    create_file_exclusive(path, zlib.compress(data, 6))


# ── reading ───────────────────────────────────────────────────────────────────


def entries(project_id: str) -> list[Edit]:
    """Every logged edit, oldest first; a line that doesn't parse is skipped."""
    return _entries(state_dir(project_id))


def _entries(folder: Path) -> list[Edit]:
    if not (folder / LOG_NAME).exists():
        return []
    try:
        with _locked(folder, shared=True):
            lines = (folder / LOG_NAME).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return _parse(lines)


def _parse(lines: list[str]) -> list[Edit]:
    out = []
    fields = set(Edit.__dataclass_fields__)
    for line in lines:
        try:
            raw = json.loads(line)
            out.append(Edit(**{k: v for k, v in raw.items() if k in fields}))
        except (ValueError, TypeError):
            continue
    return out


def find(project_id: str, edit_id: str) -> Edit | None:
    return next((e for e in entries(project_id) if e.id == edit_id), None)


def kept_version(project_id: str, version: str) -> bytes | None:
    """A version the log kept, or None when it was pruned (or never kept: a
    credential file's, past ``VOLATILE_S``)."""
    if (held := _held(version)) is not None:
        return held
    try:
        data = zlib.decompress(_blob_path(state_dir(project_id), version).read_bytes())
    except (FileNotFoundError, zlib.error):
        return None
    return data if version_of(data) == version else None


def _current_version(file: Path) -> str | None:
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


def listing(project_id: str, root: Path, *, path: str | None = None, worktree: str | None = None,
            since: datetime | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Edits newest first, for ``GET /edits``: each with ``still_current``
    (the file is still at the version it saved) and ``undoable`` (the version
    it replaced is kept, and nothing changed the file since)."""
    folder = state_dir(project_id)
    found = [e for e in reversed(_entries(folder))
             if (path is None or e.path == path or e.real == path)
             and (worktree is None or e.worktree == worktree)
             and (since is None or e.when > since)][: max(1, min(limit, LIST_MAX))]
    current: dict[str, str | None] = {}
    out = []
    for edit in found:
        if edit.real not in current:
            current[edit.real] = _current_version(root / edit.real)
        still = current[edit.real] == edit.to_version
        restorable = edit.from_version is None or _blob_path(folder, edit.from_version).exists() \
            or _held(edit.from_version) is not None
        out.append({**asdict(edit), "still_current": still, "undoable": still and restorable})
    return out


# ── what an agent engine reads ────────────────────────────────────────────────


def _project_of(path: Path) -> tuple[str, Path] | None:
    """The project a resolved path is in: ``(project id, project root)``."""
    root = xo_projects_root().resolve()
    if not path.is_relative_to(root) or path == root:
        return None
    pid = path.relative_to(root).parts[0]
    return pid, root / pid


def edits_since(folder: Path, since: datetime) -> list[dict[str, Any]]:
    """What the person changed in Files inside ``folder`` (a session's
    worktree, or a project folder) after ``since``: one entry per file,
    ``{path, edits, last_at, kind, exists}``, oldest first, ``path``
    relative to ``folder``. A project folder's own list leaves out what is
    inside its thread worktrees and XO's records (``.xo/``): those are other
    sessions' business. Empty when the folder isn't in a project."""
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
    by_path: dict[str, dict[str, Any]] = {}
    for edit in logged:
        if edit.when <= since:
            continue
        file = root / edit.real
        if not file.is_relative_to(folder):
            continue
        rel = file.relative_to(folder).as_posix()
        if rel.split("/", 1)[0] == ".xo":
            continue
        entry = by_path.setdefault(rel, {"path": rel, "edits": 0})
        entry.update(edits=entry["edits"] + 1, last_at=edit.at, kind=edit.kind,
                     exists=edit.to_version is not None)
    return sorted(by_path.values(), key=lambda e: e["last_at"])


def written_by_person(file: Path) -> bool:
    """Whether ``file``'s content now is what the person last saved there
    through Files, confirming it (``confirm: "runs_code"``): the latest
    logged edit of exactly this path, saved under this very name (not
    through a link), wrote exactly these bytes. For an agent engine's
    integrity check, so the person's confirmed edit of an agent's config
    isn't reported as the agent's. Nothing is resolved and no link is
    followed: a watched path made a link to a file the person saved isn't
    theirs."""
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
            or latest.confirmed != RUNS_CODE_CONFIRM:
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
        log.warning("project files: couldn't prune the edit log of %s", project_id, exc_info=True)


def prune(folder: Path, *, now: datetime | None = None) -> None:
    """Drop edits older than ``RETENTION_DAYS`` (and past ``MAX_LOG_LINES``),
    the versions nothing kept refers to, then the oldest versions until the
    rest fit ``MAX_BLOB_BYTES``."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=RETENTION_DAYS)
    with _locked(folder):
        logged = _parse((folder / LOG_NAME).read_text(encoding="utf-8").splitlines()) \
            if (folder / LOG_NAME).exists() else []
        kept = [e for e in logged if e.when > cutoff][-MAX_LOG_LINES:]
        if len(kept) != len(logged):
            tmp = folder / f".{LOG_NAME}.tmp"
            tmp.write_text("".join(json.dumps(asdict(e), separators=(",", ":")) + "\n" for e in kept),
                           encoding="utf-8")
            os.replace(tmp, folder / LOG_NAME)
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
