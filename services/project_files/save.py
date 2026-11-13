"""The guarded save (``PUT /file``) and undo (``POST /edits/{id}/undo``).

A save names the version it started from (``base_version``; ``None`` to
create a file that must not exist yet), and is written only if the file is
still at that version, so it never silently overwrites a newer change, an
agent's or another window's: otherwise 409 ``file_changed`` with what is
there now, for the conflict view.

**How a file is replaced.** The address is checked (``address.locate``: the
path and its links stay in the project; the place decides whether the save
is allowed, needs ``confirm: "runs_code"``, or is refused), then the
resolved path's folders are opened one by one without following a link, the
current bytes are read through a descriptor and hashed, and compared with
``base_version``. The new bytes go to a temp file beside it (created
exclusively, the old file's mode kept), are fsync'd, the target's identity
(inode, size, times) is checked against what was hashed, then renamed over
it and the folder fsync'd. A create links the temp file into place, which
fails if something appeared meanwhile. One lock per file serialises this
server's own saves; an agent takes no lock, so its write can still land in
the instants between the identity check and the rename.

The content is written as sent (line endings are the editor's), encoded as
UTF-8; a BOM the old file had is kept. Each save is logged
(``services.project_files.edits``); a save that changes nothing writes
nothing and logs nothing.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import secrets
import threading
from dataclasses import dataclass
from typing import Iterator

from services.project_files import edits
from services.project_files.address import (
    EDIT_MAX_BYTES,
    RUNS_CODE_CONFIRM,
    FileRefused,
    Place,
    Target,
    locate,
    open_file,
    parent_dir,
    read_fd,
    version_of,
)
from services.project_files.read import BOM, PREVIEW_MAX_BYTES, is_utf8_text, line_endings

log = logging.getLogger(__name__)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True)
class Saved:
    relative_path: str
    version: str | None
    size_bytes: int
    saved_at: str | None
    edit_id: str | None
    unchanged: bool


# ── the checks ────────────────────────────────────────────────────────────────


def check_place(target: Target, confirm: str | None) -> None:
    if target.place is Place.READ_ONLY:
        raise FileRefused("read_only_place", "This is one of XO's own records (or a worktree's link to its git "
                                             "data); it can be read here but not edited.", status=403)
    if target.place is Place.RUNS_CODE and confirm != RUNS_CODE_CONFIRM:
        raise FileRefused("confirm_required", "Git or an agent runs what this file says. Confirm the save "
                                              "(confirm: \"runs_code\") to write it.", status=428,
                          extra={"reason": RUNS_CODE_CONFIRM})


def encode(content: str) -> bytes:
    try:
        data = content.encode("utf-8")
    except UnicodeEncodeError:
        raise FileRefused("invalid_content", "The content isn't valid text (it holds an unpaired surrogate).",
                          status=400) from None
    if len(data) > EDIT_MAX_BYTES:
        raise FileRefused("too_large", f"Files over {EDIT_MAX_BYTES // (1024 * 1024)} MB can't be saved here.",
                          status=413)
    return data


def _changed(current: bytes | None, *, deleted: bool = False) -> FileRefused:
    payload: dict | None = None
    if current is not None:
        payload = {"version": version_of(current)}
        if len(current) <= PREVIEW_MAX_BYTES * 4 and is_utf8_text(current):
            bom = current.startswith(BOM)
            payload.update(content=current[len(BOM):].decode("utf-8") if bom else current.decode("utf-8"),
                           bom=bom, line_endings=line_endings(current))
    message = ("The file was deleted since you opened it." if deleted and current is None
               else "The file changed since you opened it (an agent or another window saved it).")
    return FileRefused("file_changed", message, status=409, extra={"current": payload})


@contextlib.contextmanager
def _file_lock(target: Target) -> Iterator[None]:
    key = str(target.path)
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:
        yield


def _identity(st: os.stat_result) -> tuple:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _tmp_name(name: str) -> str:
    return f".{name[:120]}.xo-save.{secrets.token_hex(6)}.tmp"


# ── the write ─────────────────────────────────────────────────────────────────


def _replace(target: Target, new: bytes | None, base_version: str | None, *,
             keep_bom: bool) -> tuple[bytes | None, bytes | None]:
    """Make the file hold ``new`` (``None``: remove it) if it is at
    ``base_version`` (``None``: absent). Returns ``(before, after)``."""
    name = target.real_rel.rsplit("/", 1)[-1]
    with parent_dir(target) as folder:
        fd = open_file(folder, name)
        before: bytes | None = None
        seen: tuple | None = None
        mode = None
        if fd is not None:
            try:
                st = os.fstat(fd)
                if st.st_size > EDIT_MAX_BYTES:
                    raise FileRefused("too_large", "This file is too large to edit here.", status=413)
                before = read_fd(fd, EDIT_MAX_BYTES + 1)
                after_read = os.fstat(fd)
            finally:
                os.close(fd)
            if _identity(st) != _identity(after_read) or len(before) > EDIT_MAX_BYTES:
                raise _changed(before)               # written while it was read
            seen, mode = _identity(after_read), st.st_mode & 0o7777

        if base_version is None:
            if before is not None:
                raise FileRefused("file_exists", "A file with this name already exists.", status=409,
                                  extra={"current": {"version": version_of(before)}})
        elif before is None:
            raise _changed(None, deleted=True)
        elif version_of(before) != base_version:
            raise _changed(before)
        elif not is_utf8_text(before):
            raise FileRefused("not_text", "This file isn't UTF-8 text, so it can't be edited here.", status=415)

        if new is not None and keep_bom and before is not None and before.startswith(BOM) \
                and not new.startswith(BOM):
            new = BOM + new
        if new == before:
            return before, before

        def unchanged_since_read() -> None:
            try:
                now = os.stat(name, dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                now = None
            if (now is None) != (seen is None) or (now is not None and _identity(now) != seen):
                raise _changed(_reread(folder, name))

        if new is None:
            unchanged_since_read()
            os.unlink(name, dir_fd=folder)
            _sync(folder)
            return before, None

        tmp = _tmp_name(name)
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666,
                      dir_fd=folder)
        try:
            try:
                if mode is not None:
                    os.fchmod(tfd, mode)
                view = memoryview(new)
                while view:
                    view = view[os.write(tfd, view):]
                os.fsync(tfd)
            finally:
                os.close(tfd)
            unchanged_since_read()
            if before is None:
                try:
                    os.link(tmp, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
                except FileExistsError:
                    raise FileRefused("file_exists", "A file with this name appeared while saving.",
                                      status=409) from None
                os.unlink(tmp, dir_fd=folder)
            else:
                os.replace(tmp, name, src_dir_fd=folder, dst_dir_fd=folder)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=folder)
            raise
        _sync(folder)
        return before, new


def _reread(folder: int, name: str) -> bytes | None:
    with contextlib.suppress(FileRefused, OSError):
        fd = open_file(folder, name)
        if fd is None:
            return None
        try:
            return read_fd(fd, EDIT_MAX_BYTES + 1)
        finally:
            os.close(fd)
    return None


def _sync(folder: int) -> None:
    with contextlib.suppress(OSError):
        os.fsync(folder)


def _logged(target: Target, before: bytes | None, after: bytes | None, *, confirmed: str | None,
            undoes: str | None = None) -> edits.Edit | None:
    kind = "undo" if undoes else "create" if before is None else "save"
    try:
        return edits.record(target, before, after, kind=kind, confirmed=confirmed, undoes=undoes)
    except OSError:
        log.exception("project files: saved %s but couldn't log the edit", target.rel)
        return None


# ── the routes' services ──────────────────────────────────────────────────────


def save(project_id: str, relative_path: str, content: str, base_version: str | None, *,
         confirm: str | None = None) -> Saved:
    """Save one file (see the module docstring). ``FileRefused`` for every
    refusal, with the status the route answers."""
    data = encode(content)
    target = locate(project_id, relative_path)
    check_place(target, confirm)
    with _file_lock(target):
        try:
            before, after = _replace(target, data, base_version, keep_bom=True)
        except OSError as exc:
            raise _os_failure(exc) from exc
        if after == before:
            return Saved(target.rel, version_of(after), len(after), None, None, True)
        # Logged under the file's lock: the log's order is the file's.
        edit = _logged(target, before, after, confirmed=confirm if target.place is Place.RUNS_CODE else None)
    return Saved(target.rel, version_of(after), len(after), edit.at if edit else None, edit.id if edit else None,
                 False)


def undo(project_id: str, edit_id: str) -> Saved:
    """Put back what one edit replaced, if the file is still as that edit
    left it (409 ``file_changed`` otherwise). Undoing a create removes the
    file; an undo can itself be undone. The person confirmed the edit, so
    its undo needs no second confirmation; XO's records stay refused."""
    edit = edits.find(project_id, edit_id)
    if edit is None:
        raise FileRefused("edit_not_found", "There is no such edit in this project's log.", status=404)
    target = locate(project_id, edit.path)
    if target.real_rel != edit.real:
        raise FileRefused("place_changed", "This path leads to another file than the one edited.", status=409)
    check_place(target, RUNS_CODE_CONFIRM)
    previous = None
    if edit.from_version is not None:
        previous = edits.kept_version(project_id, edit.from_version)
        if previous is None:
            raise FileRefused("not_undoable", "The version before this edit is no longer kept, so it can't be "
                                              "undone.", status=410)
    with _file_lock(target):
        try:
            # ``to_version`` None: the edit removed the file, which must still be absent.
            before, after = _replace(target, previous, edit.to_version, keep_bom=False)
        except OSError as exc:
            raise _os_failure(exc) from exc
        version = version_of(after) if after is not None else None
        if after == before:
            return Saved(target.rel, version, len(after or b""), None, None, True)
        logged = _logged(target, before, after, confirmed=edit.confirmed, undoes=edit.id)
    return Saved(target.rel, version, len(after or b""), logged.at if logged else None,
                 logged.id if logged else None, False)


def _os_failure(exc: OSError) -> FileRefused:
    if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
        return FileRefused("not_writable", "Space can't write this file (permission denied or a read-only "
                                           "disk).", status=403)
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return FileRefused("no_space", "The disk is full, so the file wasn't saved.", status=507)
    if exc.errno == errno.ENAMETOOLONG:
        return FileRefused("invalid_relative_path", "The file name is too long.", status=400)
    return FileRefused("write_failed", f"The file couldn't be written ({exc.strerror or exc}).", status=500)
