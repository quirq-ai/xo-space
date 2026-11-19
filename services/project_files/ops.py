"""The person's file operations in Files: new folder, rename (and move),
delete into Space's trash, and undoing a rename or a delete
(``POST /files/mkdir | rename | delete``, ``POST /edits/{id}/undo``).

**Where.** An entry's folder is resolved and must be in the project; the
entry itself is never followed (``address.locate_entry``), so a link is
moved or removed as a link. Folders are opened one at a time without
following a link (``address.parent_dir``); missing folders on the way to a
new name are made the same way. A move never replaces what is at its new
name (``renameat2(RENAME_NOREPLACE)``, else a check right before).

**The places rules, at both ends.** A move or a delete changes every file
under the entry: the strictest place of any of them, at its old path and at
its new one, decides (403 ``read_only_place``, 428 ``confirm_required``
until ``confirm: "runs_code"``).

**What is kept.** Each operation gets an id (its ``edit_id``) and a line in
the operations journal (``<state>/projects/<pid>/files/ops/<id>.json``), and
writes one version record per file it moved or removed, tied by that id
(``group``; a one-file operation's record has the id itself). A delete
moves the entry into ``<state>/projects/<pid>/files/trash/<id>/``, never
into the project; a credential-looking file (``edits.SECRET_NAMES``) is not
kept there but in memory for ``VOLATILE_S``, as its versions are. The
journal and the trash follow the store's retention.

**Undo.** A rename moves back (it can be undone again); a delete comes back
from the trash, if nothing sits at its name (409 ``exists``). The person
confirmed the operation, so its undo needs no second confirmation; XO's
records stay refused.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import json
import logging
import os
import shutil
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from services.project_files import edits
from services.project_files.address import (
    RUNS_CODE_CONFIRM,
    FileRefused,
    Place,
    Target,
    classify,
    invalid_path_code,
    is_noise,
    locate_entry,
    parent_dir,
    place_changed,
    project_root,
    version_of,
)
from services.project_files.save import Saved, _changed, _os_failure, check_place, file_locks

log = logging.getLogger(__name__)

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
_SUBDIR = _DIR | os.O_NOFOLLOW
#: The trash's one entry per operation (the name it had is in the journal).
TRASH_ITEM = "item"
#: A credential file larger than this isn't held for undo: it is removed.
SECRET_HOLD_MAX = 16 * 1024 * 1024
RENAME_NOREPLACE = 1


@dataclass(frozen=True)
class Moved:
    relative_path: str
    old_relative_path: str
    kind: str
    edit_id: str
    version: str | None


@dataclass(frozen=True)
class Deleted:
    edit_id: str
    kind: str
    count: int


def not_found() -> FileRefused:
    return FileRefused("not_found", "There is no file or folder at this path.", status=404)


def exists(current: dict | None = None) -> FileRefused:
    return FileRefused("exists", "Something already has this name.", status=409,
                       extra={"current": current} if current is not None else None)


def _entry(project_id: str, relative_path: str) -> Target:
    try:
        return locate_entry(project_id, relative_path)
    except FileRefused as exc:
        raise invalid_path_code(exc) from None


# ── walking and moving without following links ──────────────────────────────


@contextlib.contextmanager
def _made_parents(target: Target) -> Iterator[int]:
    """``parent_dir``, making the folders that are missing on the way."""
    opened: list[int] = []
    try:
        opened.append(os.open(target.root, _DIR))
        for name in target.real_rel.split("/")[:-1]:
            try:
                opened.append(os.open(name, _SUBDIR, dir_fd=opened[-1]))
                continue
            except FileNotFoundError:
                pass
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise place_changed() from None
                raise
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, 0o777, dir_fd=opened[-1])
            try:
                opened.append(os.open(name, _SUBDIR, dir_fd=opened[-1]))
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise place_changed() from None
                raise
        yield opened[-1]
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _lstat(target: Target) -> os.stat_result | None:
    try:
        with parent_dir(target) as folder:
            return os.stat(target.real_rel.rsplit("/", 1)[-1], dir_fd=folder, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except FileRefused as exc:
        if exc.code == "folder_not_found":
            return None
        raise


try:
    _renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
except OSError:                                         # no C library to ask: the check-then-rename below
    _renameat2 = None


def _rename_noreplace(src_dir: int, src: str, dst_dir: int, dst: str) -> None:
    """Move ``src`` to ``dst`` unless something is there (``FileExistsError``)."""
    if _renameat2 is not None:
        if _renameat2(src_dir, os.fsencode(src), dst_dir, os.fsencode(dst), RENAME_NOREPLACE) == 0:
            return
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL):
            raise OSError(code, os.strerror(code))
    try:
        os.stat(dst, dir_fd=dst_dir, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(src, dst, src_dir_fd=src_dir, dst_dir_fd=dst_dir)
        return
    raise FileExistsError(errno.EEXIST, "exists", dst)


def _move(src_dir: int, src: str, src_path: Path, dst_dir: int, dst: str, dst_path: Path) -> None:
    """Move one entry; across file systems, copy it (links as links) and
    remove the original."""
    try:
        _rename_noreplace(src_dir, src, dst_dir, dst)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    st = os.stat(src, dir_fd=src_dir, follow_symlinks=False)
    if stat.S_ISDIR(st.st_mode):
        shutil.copytree(src_path, dst_path, symlinks=True)
        shutil.rmtree(src_path)
    else:
        shutil.copy2(src_path, dst_path, follow_symlinks=False)
        os.unlink(src, dir_fd=src_dir)


def _walk(top: Path) -> tuple[list[str], list[str]]:
    """``(folders, files)`` under ``top``, relative to it, never following
    a link (a link is listed as a file)."""
    folders: list[str] = []
    files: list[str] = []
    for current, dirnames, filenames in os.walk(top, followlinks=False):
        base = Path(current).relative_to(top).as_posix()
        prefix = "" if base == "." else base + "/"
        for name in list(dirnames):
            if os.path.islink(os.path.join(current, name)):
                dirnames.remove(name)
                files.append(prefix + name)
            else:
                folders.append(prefix + name)
        files += [prefix + name for name in filenames]
    return folders, files


def _place(*pairs: tuple[Target, list[str]]) -> Place:
    """The strictest place of every path under these entries, as written and
    as resolved."""
    worst = Place.ORDINARY
    for target, inner in pairs:
        for base in dict.fromkeys((target.rel, target.real_rel)):
            worst = max(worst, classify(base))
            for rel in inner:
                worst = max(worst, classify(f"{base}/{rel}"))
                if worst is Place.READ_ONLY:
                    return worst
    return worst


def _check(place: Place, confirm: str | None) -> None:
    check_place(Target("", Path("/"), "", "", place), confirm)


# ── the journal ──────────────────────────────────────────────────────────────


def _journal_path(project_id: str, op_id: str) -> Path:
    return edits.state_dir(project_id) / edits.OPS_DIRNAME / f"{op_id}.json"


def _write_journal(project_id: str, entry: dict[str, Any]) -> None:
    path = _journal_path(project_id, entry["id"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _read_journal(project_id: str, op_id: str) -> dict[str, Any] | None:
    if not op_id or "/" in op_id or op_id.startswith("."):
        return None
    try:
        return json.loads(_journal_path(project_id, op_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _record(project_id: str, changes: list[edits.Change]) -> None:
    try:
        edits.write(project_id, changes)
    except OSError:
        log.exception("project files: moved files in %s but couldn't record the versions", project_id)


def _versions(op_id: str, base: Target, files: list[str], top: Path, *, kind: str, old: Target | None = None,
              single: bool, confirmed: str | None, undoes: str | None = None, gone: bool = False
              ) -> tuple[list[edits.Change], edits.Content | None]:
    """One change per versioned file under ``top`` (``base``'s files now,
    or the trash's copy for a delete)."""
    changes: list[edits.Change] = []
    first: edits.Content | None = None
    for inner in files:
        rel = base.rel if single else f"{base.rel}/{inner}"
        real = base.real_rel if single else f"{base.real_rel}/{inner}"
        if is_noise(real):
            continue
        content = edits.content_at(top if single else top / inner)
        if content is None:
            continue
        first = first or content
        changes.append(edits.Change(
            path=rel, real=real, kind=kind,
            before=content, after=None if gone else content, by=edits.writer(edits.UI),
            old_path=None if old is None else (old.rel if single else f"{old.rel}/{inner}"),
            old_real=None if old is None else (old.real_rel if single else f"{old.real_rel}/{inner}"),
            confirmed=confirmed, undoes=undoes, group=op_id, id=op_id if single else None, mergeable=False))
    return changes, first


# ── the operations ───────────────────────────────────────────────────────────


def mkdir(project_id: str, relative_path: str, *, confirm: str | None = None) -> str:
    """Make a folder (and the folders on the way); 409 ``exists``."""
    target = _entry(project_id, relative_path)
    check_place(target, confirm)
    if _lstat(target) is not None:
        raise exists()
    try:
        with _made_parents(target) as folder:
            os.mkdir(target.real_rel.rsplit("/", 1)[-1], 0o777, dir_fd=folder)
    except FileExistsError:
        raise exists() from None
    except OSError as exc:
        raise _os_failure(exc) from exc
    return target.rel


def rename(project_id: str, relative_path: str, new_relative_path: str, *, confirm: str | None = None) -> Moved:
    """Rename or move a file or folder (missing folders on the way are
    made). See the module docstring."""
    return _rename(project_id, relative_path, new_relative_path, confirm=confirm)


def _rename(project_id: str, relative_path: str, new_relative_path: str, *, confirm: str | None,
            undoes: str | None = None) -> Moved:
    src = _entry(project_id, relative_path)
    dst = _entry(project_id, new_relative_path)
    for a, b in ((src.rel, dst.rel), (src.real_rel, dst.real_rel)):
        if b == a or b.startswith(a + "/"):
            raise FileRefused("invalid_path", "A file or folder can't be moved onto or into itself.", status=400)
    st = _lstat(src)
    if st is None:
        raise not_found()
    is_dir = stat.S_ISDIR(st.st_mode)
    folders, files = _walk(src.path) if is_dir else ([], [""])
    inner = folders + [f for f in files if f]
    place = _place((src, inner), (dst, inner))
    _check(place, confirm)
    confirmed = confirm if place is Place.RUNS_CODE else None
    op_id = edits.new_id()
    paths = [src.path] + [src.path / f for f in files if f] + [dst.path] + [dst.path / f for f in files if f]
    with file_locks(paths):
        current = _lstat(dst)
        if current is not None:
            raise exists(_current(dst.path, current))
        try:
            with parent_dir(src) as sfd, _made_parents(dst) as dfd:
                _move(sfd, src.real_rel.rsplit("/", 1)[-1], src.path, dfd, dst.real_rel.rsplit("/", 1)[-1], dst.path)
        except FileExistsError:
            raise exists(_current(dst.path, _lstat(dst))) from None
        except FileNotFoundError:
            raise not_found() from None
        except OSError as exc:
            raise _os_failure(exc) from exc
        changes, first = _versions(op_id, dst, files if is_dir else [""], dst.path,
                                   kind="undo" if undoes else "rename", old=src, single=not is_dir,
                                   confirmed=confirmed, undoes=undoes)
        _write_journal(project_id, {"id": op_id, "kind": "rename", "at": edits.now_precise(), "path": dst.rel,
                                    "old_path": src.rel, "folder": is_dir, "undoes": undoes})
        _record(project_id, changes)
    return Moved(dst.rel, src.rel, "folder" if is_dir else "file", op_id,
                 None if is_dir or first is None else first.version)


def _current(path: Path, st: os.stat_result | None) -> dict | None:
    if st is None or not stat.S_ISREG(st.st_mode):
        return None
    found = edits.content_at(path)
    return {"version": found.version} if found else None


def delete(project_id: str, relative_path: str, *, base_version: str | None = None,
           confirm: str | None = None) -> Deleted:
    """Move a file or folder into Space's trash. See the module docstring."""
    target = _entry(project_id, relative_path)
    st = _lstat(target)
    if st is None:
        raise not_found()
    is_dir = stat.S_ISDIR(st.st_mode)
    folders, files = _walk(target.path) if is_dir else ([], [""])
    inner = folders + [f for f in files if f]
    place = _place((target, inner))
    _check(place, confirm)
    confirmed = confirm if place is Place.RUNS_CODE else None
    op_id = edits.new_id()
    trash = edits.state_dir(project_id) / edits.TRASH_DIRNAME / op_id
    with file_locks([target.path] + [target.path / f for f in files if f]):
        if base_version is not None:
            now = None if is_dir else edits.content_at(target.path)
            if now is None or now.version != base_version:
                if now is not None and now.data is not None:
                    raise _changed(now.data)
                raise FileRefused("file_changed", "This changed since you opened it (it is a folder now, or "
                                                  "something else wrote it).", status=409,
                                  extra={"current": {"version": now.version} if now is not None else None})
        trash.mkdir(parents=True, mode=0o700)
        try:
            with parent_dir(target) as sfd:
                dfd = os.open(trash, _DIR)
                try:
                    _move(sfd, target.real_rel.rsplit("/", 1)[-1], target.path, dfd, TRASH_ITEM, trash / TRASH_ITEM)
                finally:
                    os.close(dfd)
        except FileNotFoundError:
            shutil.rmtree(trash, ignore_errors=True)
            raise not_found() from None
        except OSError as exc:
            shutil.rmtree(trash, ignore_errors=True)
            raise _os_failure(exc) from exc
        item = trash / TRASH_ITEM
        changes, _first = _versions(op_id, target, files if is_dir else [""], item, kind="delete",
                                    single=not is_dir, confirmed=confirmed, gone=True)
        held = _hold_secrets(item, is_dir, target.name)
        _write_journal(project_id, {"id": op_id, "kind": "delete", "at": edits.now_precise(), "path": target.rel,
                                    "folder": is_dir, "secrets": held})
        _record(project_id, changes)
    return Deleted(op_id, "folder" if is_dir else "file", len([f for f in files if f]) if is_dir else 1)


def _hold_secrets(item: Path, is_dir: bool, name: str) -> list[dict[str, Any]]:
    """Take the credential files out of the trash into memory (for
    ``VOLATILE_S``): ``[{inner, version, mode}]`` (``inner`` empty for a
    lone file), ``version`` None when it was too large to hold and is
    gone."""
    found = [(f, item / f, f.rsplit("/", 1)[-1]) for f in _walk(item)[1]] if is_dir else [("", item, name)]
    return [_take_secret(inner, path) for inner, path, base in found if edits.is_secret_name(base)]


def _take_secret(inner: str, path: Path) -> dict[str, Any]:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"inner": inner, "version": None, "mode": None}
    version = None
    if stat.S_ISREG(st.st_mode) and st.st_size <= SECRET_HOLD_MAX:
        data = path.read_bytes()
        edits.hold(data)
        version = version_of(data)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    return {"inner": inner, "version": version, "mode": stat.S_IMODE(st.st_mode)}


# ── undo ─────────────────────────────────────────────────────────────────────


def undo(project_id: str, op_id: str) -> Saved | None:
    """Undo one rename or delete; None when the journal has no such
    operation."""
    project_root(project_id)
    entry = _read_journal(project_id, op_id)
    if entry is None:
        return None
    if entry.get("kind") == "rename":
        return _move_back(project_id, entry)
    if entry.get("kind") == "delete":
        return _restore(project_id, entry)
    raise FileRefused("not_undoable", "A file brought back from the trash can't be undone; delete it again "
                                      "instead.", status=409)


def _move_back(project_id: str, entry: dict[str, Any]) -> Saved:
    try:
        moved = _rename(project_id, entry["path"], entry["old_path"], confirm=RUNS_CODE_CONFIRM,
                        undoes=entry["id"])
    except FileRefused as exc:
        if exc.code == "not_found":
            raise FileRefused("not_found", "Nothing is at the new name any more, so there is nothing to move back.",
                              status=404) from None
        raise
    return Saved(moved.relative_path, moved.version, 0, edits.now_precise(), moved.edit_id, False)


def _restore(project_id: str, entry: dict[str, Any]) -> Saved:
    """Bring a delete back from the trash. A credential file comes back from
    memory: it is written into the trash's copy only once every check has
    passed, under the files' locks, and taken out again if the move fails,
    so nothing of it stays in Space's state."""
    target = _entry(project_id, entry["path"])
    check_place(target, RUNS_CODE_CONFIRM)
    trash = edits.state_dir(project_id) / edits.TRASH_DIRNAME / entry["id"]
    item = trash / TRASH_ITEM
    is_dir = bool(entry.get("folder"))
    secrets_back: list[tuple[Path, bytes, int]] = []
    missing = []
    for secret in entry.get("secrets") or []:
        data = edits.held(secret["version"]) if secret.get("version") else None
        if data is None:
            missing.append(secret["inner"])
            continue
        secrets_back.append((item / secret["inner"] if secret["inner"] else item, data, secret.get("mode") or 0o600))
    if not os.path.lexists(item) and not secrets_back:
        raise FileRefused("not_undoable", "The deleted copy is no longer kept (it was pruned, or it was a "
                                          "credential file Space no longer holds), so it can't be brought back.",
                          status=410)
    folders, files = _walk(item) if is_dir and os.path.lexists(item) else ([], [] if is_dir else [""])
    inner = folders + [f for f in files if f] + [str(p.relative_to(item)) for p, _d, _m in secrets_back if p != item]
    _check(_place((target, inner)), RUNS_CODE_CONFIRM)
    op_id = edits.new_id()
    with file_locks([target.path] + [target.path / f for f in inner]):
        current = _lstat(target)
        if current is not None:
            raise exists(_current(target.path, current))
        written: list[Path] = []
        try:
            if is_dir:
                item.mkdir(parents=True, exist_ok=True, mode=0o700)
            for path, data, mode in secrets_back:
                if os.path.lexists(path):
                    continue
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
                written.append(path)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
            with _made_parents(target) as dfd:
                sfd = os.open(trash, _DIR)
                try:
                    _move(sfd, TRASH_ITEM, item, dfd, target.real_rel.rsplit("/", 1)[-1], target.path)
                finally:
                    os.close(sfd)
        except BaseException as exc:
            for path in written:                         # the credentials go back to memory only
                with contextlib.suppress(OSError):
                    os.unlink(path)
            if isinstance(exc, FileExistsError):
                raise exists(_current(target.path, _lstat(target))) from None
            if isinstance(exc, OSError):
                raise _os_failure(exc) from exc
            raise
        folders, files = _walk(target.path) if is_dir else ([], [""])
        changes, first = _versions(op_id, target, files if is_dir else [""], target.path, kind="undo",
                                   single=not is_dir, confirmed=None, undoes=entry["id"])
        changes = [replace(c, before=None) for c in changes]
        _write_journal(project_id, {"id": op_id, "kind": "restore", "at": edits.now_precise(), "path": target.rel,
                                    "undoes": entry["id"], "missing": missing})
        _record(project_id, changes)
    shutil.rmtree(trash, ignore_errors=True)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(_journal_path(project_id, entry["id"]))
    return Saved(target.rel, None if is_dir or first is None else first.version,
                 0 if is_dir or first is None else first.size, edits.now_precise(), op_id, False)
