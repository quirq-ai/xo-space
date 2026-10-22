"""Files the owner attaches to a message (hub #30).

The server picks every path; nothing a client or an agent sends names one.
A file is reached only as ``(chat, att_id)``:

    <chat folder>/.xo/attachments/<att_id>/file.<ext>   the bytes
    <chat folder>/.xo/attachments/<att_id>/meta.json    name, type, bytes, sha256, chat_id, created_at

The chat folder is the projects root for the Workspace and the project's own
folder otherwise (``chat_folder``). ``.xo/`` is XO's: agents can't write
there (``.xo/**`` is protected), so no agent can swap a file; they read it
at the path their envelope names (``storm/review/paths.py`` counts their own
project's attachments as inside their folder, for reads only).

What keeps it from becoming a file-system surface:

- an id must match ``att_<24 hex>`` before anything touches the disk, and is
  looked up only in the folder of the chat named in the request;
- the person's file name is display only: on disk the file is always
  ``file.<ext>``, the extension chosen from the sniffed type;
- the type is decided by the bytes (PNG, JPEG, GIF, WebP, PDF, or UTF-8 text
  with no NUL), never by the client's label or the name;
- a symlinked or non-folder ``.xo``, attachments folder or id folder is
  refused, every file is opened with ``O_NOFOLLOW``, and every resolved path
  must stay inside the resolved attachments folder;
- an upload is streamed to disk and stopped past ``MAX_BYTES``; a chat holds
  at most ``UNSENT_MAX_FILES`` / ``UNSENT_MAX_BYTES`` of files no message
  names yet, and the sweep removes those older than ``UNSENT_KEEP_S``.

Blocking: every function here runs on a worker thread (``asyncio.to_thread``),
except ``carry``, which hands its copying to one.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from services.cowork_agent import project_layout
from services.cowork_agent.storm.engine.model import IMAGE_INPUT_MAX_BYTES
from services.errors import ServiceError
from services.storage.atomic_write import create_file_exclusive
from services.xo_structure import XO_DIRNAME

ATTACHMENTS_DIRNAME = "attachments"
#: One file at most (the owner's decision, 2026-10-22).
MAX_BYTES = 25 * 1024 * 1024
#: Files one message carries at most.
MAX_PER_MESSAGE = 10
#: What a chat may hold that no message names yet (uploads not sent).
UNSENT_MAX_FILES = 100
UNSENT_MAX_BYTES = 500 * 1024 * 1024
#: An upload no message names is removed after this long.
UNSENT_KEEP_S = 24 * 3600
#: The longest display name kept, in UTF-8 bytes.
NAME_MAX_BYTES = 255
#: The read and copy unit.
CHUNK = 1024 * 1024

#: Every type taken, with the extension its file gets on disk.
TYPES: Mapping[str, str] = {
    "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
    "application/pdf": "pdf", "text/plain": "txt", "text/markdown": "md",
}
IMAGE_TYPES = frozenset(t for t in TYPES if t.startswith("image/"))
#: Shown inline by the read route; everything else downloads.
INLINE_TYPES = IMAGE_TYPES | {"application/pdf"}

_ID = re.compile(r"^att_[0-9a-f]{24}$")
_MARKDOWN = (".md", ".markdown", ".mdown")
_SNIFF_BYTES = 12
_FILE = "file"
_META = "meta.json"
_PART = "upload.part"
_IGNORE = ".gitignore"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

log = logging.getLogger(__name__)


class AttachmentRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(code, message, status=status)


def _not_found(att_id: str) -> AttachmentRefused:
    return AttachmentRefused("attachment_not_found", f"There is no attachment {att_id!r} in this chat.", status=404)


# ── where ────────────────────────────────────────────────────────────────────

def chat_folder(chat: Mapping[str, Any]) -> Path:
    """Where the chat's files live: the projects root for the Workspace, the
    project folder for a project (the coordinator's own folder)."""
    root = Path(os.path.realpath(project_layout.xo_projects_root()))
    return root if chat["kind"] == "workspace" else root / chat["project"]


def _real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _refuse_unsafe(path: Path) -> None:
    """``path`` is a real folder or absent; a symlink or a file in its place
    is refused, never followed (a cloned repository may commit either)."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(mode):
        raise AttachmentRefused("unsafe_attachments_folder", f"{path} is a symlink or not a folder, so XO won't keep "
                                                             "files there. Remove it and try again.", status=409)


def attachments_root(folder: Path, *, create: bool) -> Path | None:
    """``<folder>/.xo/attachments``, checked; made (with a ``.gitignore``
    holding ``*``, so git never sees a file) when ``create``. None when it
    doesn't exist and isn't to be made."""
    folder = Path(os.path.realpath(folder))
    xo, root = folder / XO_DIRNAME, folder / XO_DIRNAME / ATTACHMENTS_DIRNAME
    for place in (xo, root):
        _refuse_unsafe(place)
    if not _real_dir(root):
        if not create:
            return None
        if not _real_dir(folder):
            raise AttachmentRefused("chat_folder_missing", f"The chat's folder {folder} doesn't exist.", status=409)
        for place in (xo, root):
            try:
                os.mkdir(place, 0o700)
            except FileExistsError:
                pass
            _refuse_unsafe(place)             # lost a race to a link: still refused
    create_file_exclusive(root / _IGNORE, b"*\n")
    return root


def path_of(folder: Path, att: Mapping[str, Any]) -> Path:
    """The absolute path an agent reads ``att`` at (an envelope's ``path``)."""
    return Path(os.path.realpath(folder)) / XO_DIRNAME / ATTACHMENTS_DIRNAME / att["id"] / \
        f"{_FILE}.{TYPES.get(att['type'], 'txt')}"


def _inside(root: Path, path: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
    except ValueError:
        return False
    return True


# ── names and types ──────────────────────────────────────────────────────────

def clean_name(raw: str | None) -> str:
    """A display name: its last component only (``/`` and ``\\``), no control
    or format characters, at most ``NAME_MAX_BYTES``. Never a path."""
    name = re.split(r"[/\\]", raw or "")[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp")).strip()
    if name in ("", ".", ".."):
        return "file"
    encoded = name.encode("utf-8")[:NAME_MAX_BYTES]
    return encoded.decode("utf-8", errors="ignore") or "file"


def _binary_type(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def public(meta: Mapping[str, Any]) -> dict[str, Any]:
    """What an API answer and a stored message carry: never a path."""
    return {"id": meta["id"], "name": meta["name"], "type": meta["type"], "bytes": meta["bytes"]}


# ── an upload ────────────────────────────────────────────────────────────────

@dataclass
class Upload:
    """One file being received: ``feed`` each chunk, then ``finish`` (or
    ``abort``). The bytes go to a private part file in a new id folder; the
    type is decided from them as they arrive."""

    folder: Path
    chat_id: str
    name: str
    id: str = field(default_factory=lambda: f"att_{secrets.token_hex(12)}")
    size: int = 0
    _dir: Path | None = None
    _fd: int | None = None
    _head: bytes = b""
    _type: str | None = None
    _text: Any = None
    _sha: Any = field(default_factory=hashlib.sha256)

    def open(self) -> None:
        root = attachments_root(self.folder, create=True)
        self._dir = root / self.id
        os.mkdir(self._dir, 0o700)
        self._fd = os.open(self._dir / _PART, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.size += len(chunk)
        if self.size > MAX_BYTES:
            raise AttachmentRefused("too_large", f"A file may be at most {MAX_BYTES // (1024 * 1024)} MB.",
                                    status=413)
        if self._type is None and self._text is None:
            self._head += chunk                 # until the type is known: the first few bytes
            if len(self._head) >= _SNIFF_BYTES:
                self._decide()
        elif self._text is not None:
            self._check_text(chunk)
        self._sha.update(chunk)
        view = memoryview(chunk)
        while view:
            view = view[os.write(self._fd, view):]

    def _decide(self) -> None:
        self._type = _binary_type(self._head[:_SNIFF_BYTES])
        if self._type is None:
            self._text = codecs.getincrementaldecoder("utf-8")()
            self._check_text(self._head)
        self._head = b""

    def _check_text(self, chunk: bytes) -> None:
        if b"\x00" in chunk:
            raise _unsupported()
        try:
            self._text.decode(chunk)
        except UnicodeDecodeError:
            raise _unsupported() from None

    def finish(self) -> dict[str, Any]:
        if self.size == 0:
            raise AttachmentRefused("empty_file", "The file is empty.")
        if self._type is None and self._text is None:
            self._decide()                      # shorter than the sniffed head
        if self._text is not None:
            try:
                self._text.decode(b"", final=True)
            except UnicodeDecodeError:
                raise _unsupported() from None
            self._type = "text/markdown" if self.name.lower().endswith(_MARKDOWN) else "text/plain"
        os.fsync(self._fd)
        os.close(self._fd)
        self._fd = None
        os.rename(self._dir / _PART, self._dir / f"{_FILE}.{TYPES[self._type]}")
        meta = {"id": self.id, "name": self.name, "type": self._type, "bytes": self.size,
                "sha256": self._sha.hexdigest(), "chat_id": self.chat_id, "created_at": time.time()}
        _write_meta(self._dir, meta)
        return meta

    def abort(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._dir is not None and _real_dir(self._dir):
            shutil.rmtree(self._dir, ignore_errors=True)


def _unsupported() -> AttachmentRefused:
    return AttachmentRefused("unsupported_type", "Only text, images (PNG, JPEG, GIF, WebP) and PDFs can be attached.",
                             status=415)


def _write_meta(folder: Path, meta: Mapping[str, Any]) -> None:
    fd = os.open(folder / _META, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        json.dump(meta, fp)


# ── reading ──────────────────────────────────────────────────────────────────

def lookup(folder: Path, att_id: str) -> dict[str, Any]:
    """``att_id``'s metadata in this chat's folder, checked; 404 for an id of
    the wrong shape, another chat's, or a file that isn't there any more."""
    if not isinstance(att_id, str) or not _ID.match(att_id):
        raise _not_found(str(att_id)[:64])
    root = attachments_root(folder, create=False)
    if root is None:
        raise _not_found(att_id)
    folder_ = root / att_id
    if not _real_dir(folder_) or not _inside(root, folder_):
        raise _not_found(att_id)
    try:
        fd = os.open(folder_ / _META, os.O_RDONLY | _NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as fp:
            meta = json.load(fp)
    except (OSError, ValueError):
        raise _not_found(att_id) from None
    if not isinstance(meta, dict) or meta.get("id") != att_id or meta.get("type") not in TYPES:
        raise _not_found(att_id)
    return meta


def open_file(folder: Path, meta: Mapping[str, Any]) -> int:
    """A read-only descriptor on ``meta``'s bytes, opened without following a
    link; the caller closes it."""
    path = path_of(folder, meta)
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError:
        raise _not_found(meta["id"]) from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise _not_found(meta["id"])
    return fd


def read_chunks(fd: int) -> Iterator[bytes]:
    try:
        while chunk := os.read(fd, CHUNK):
            yield chunk
    finally:
        os.close(fd)


def resolve(folder: Path, att_ids: Iterable[str]) -> list[dict[str, Any]]:
    """The attachments a message names, in order; any unknown id refuses
    them all (``400 unknown_attachment``)."""
    found = []
    for att_id in att_ids:
        try:
            found.append(public(lookup(folder, att_id)))
        except AttachmentRefused as exc:
            if exc.code != "attachment_not_found":
                raise
            raise AttachmentRefused("unknown_attachment", f"There is no attachment {str(att_id)[:64]!r} in this "
                                                          "chat: upload it here first.") from None
    return found


def copy_to(src: Path, atts: Iterable[Mapping[str, Any]], dst: Path, chat_id: str) -> list[dict[str, Any]]:
    """Copies of ``atts`` (from ``src``'s chat) into ``dst``'s chat, each with
    a new id: a forwarded message's files, read in the project that gets it."""
    copied = []
    for att in atts:
        meta = lookup(src, att["id"])
        upload = Upload(folder=dst, chat_id=chat_id, name=meta["name"])
        fd = open_file(src, meta)
        try:
            upload.open()
            for chunk in read_chunks(fd):
                upload.feed(chunk)
            fresh = upload.finish()
        except BaseException:
            upload.abort()
            raise
        copied.append(public(fresh))
    return copied


def routing_text(message: Mapping[str, Any]) -> str:
    """What a decision model (a third party) and a thread title read of a
    message: its text, and its files as a count and their types, never their
    names or contents."""
    files = (message.get("extra") or {}).get("attachments") or ()
    if not files:
        return message["text"]
    kinds = ", ".join(sorted({f["type"] for f in files}))
    noun = "attachment" if len(files) == 1 else "attachments"
    return f"{message['text']}\n[{len(files)} {noun}: {kinds}]".strip()


def image_paths(folder: Path, atts: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """The images among ``atts`` small enough to go to the model as image
    input."""
    return tuple(str(path_of(folder, a)) for a in atts
                 if a.get("type") in IMAGE_TYPES and int(a.get("bytes") or 0) <= IMAGE_INPUT_MAX_BYTES)


# ── keeping ──────────────────────────────────────────────────────────────────

def _held(folder: Path) -> Iterator[tuple[str, Path]]:
    root = attachments_root(folder, create=False)
    if root is None:
        return
    with os.scandir(root) as entries:
        for entry in entries:
            if _ID.match(entry.name) and entry.is_dir(follow_symlinks=False):
                yield entry.name, Path(entry.path)


def unsent(folder: Path, named: set[str]) -> tuple[int, int]:
    """How many files, and how many bytes, the chat holds that no message
    names."""
    count = size = 0
    for att_id, _path in _held(folder):
        if att_id in named:
            continue
        count += 1
        try:
            size += int(lookup(folder, att_id)["bytes"])
        except (AttachmentRefused, KeyError, ValueError):
            pass
    return count, size


def check_room(folder: Path, named: set[str], incoming: int | None) -> None:
    count, size = unsent(folder, named)
    if count >= UNSENT_MAX_FILES or size + (incoming or 0) > UNSENT_MAX_BYTES:
        raise AttachmentRefused("too_many_unsent", f"This chat already holds {count} files no message names yet; "
                                                   "send or remove some first.", status=429)


def sweep(folder: Path, named: set[str], *, now: float) -> int:
    """Remove the files no message names, once older than ``UNSENT_KEEP_S``
    (an upload never sent, a copy whose forward failed). Only ``att_*``
    folders inside the attachments folder; a link is never followed."""
    removed = 0
    for att_id, path in list(_held(folder)):
        if att_id in named:
            continue
        try:
            born = float(lookup(folder, att_id).get("created_at") or 0)
        except AttachmentRefused:
            born = os.lstat(path).st_mtime        # no readable meta: an upload cut short
        if now - born >= UNSENT_KEEP_S:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


async def carry(chats: Any, originals: Iterable[Mapping[str, Any]], target: Mapping[str, Any]) -> dict[str, list]:
    """The files of the owner's messages about to be copied into ``target``'s
    chat (a forward, a new project's first request), copied there first:
    message id -> the copies' views. A file that can't be copied is left out
    and logged; the message still goes."""
    out: dict[str, list] = {}
    dst = chat_folder(target)
    for message in originals:
        files = (message.get("extra") or {}).get("attachments") or ()
        if not files:
            continue
        src = chat_folder(await chats.chat(message["chat_id"]))
        copied = []
        for att in files:
            try:
                copied += await asyncio.to_thread(copy_to, src, [att], dst, target["id"])
            except (AttachmentRefused, OSError) as exc:
                log.warning("storm attachments: %s of message %s wasn't copied to chat %s: %s", att.get("id"),
                            message["id"], target["id"], exc)
        out[message["id"]] = copied
    return out


#: How often the sweep runs, and how long after boot it first does.
SWEEP_INTERVAL_S = 3600.0
SWEEP_FIRST_S = 600.0


async def sweep_all(chats: Any) -> int:
    """One pass of the sweep over every current chat's folder."""
    from services.cowork_agent.storm.chats import rows

    listed = await chats.engine.store.read(lambda conn: [
        (rows.chat_view(r), rows.attachment_ids(conn, r[0]))
        for r in conn.execute(f"SELECT {rows.CHAT_COLUMNS} FROM chats WHERE retired_at IS NULL").fetchall()])
    removed = 0
    for chat, named in listed:
        try:
            removed += await asyncio.to_thread(sweep, chat_folder(chat), named, now=time.time())
        except (AttachmentRefused, OSError) as exc:
            log.warning("storm attachments: chat %s's folder wasn't swept: %s", chat["id"], exc)
    return removed
