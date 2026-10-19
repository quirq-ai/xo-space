"""A chat's memory: what a coordinator learned that must outlive its
incarnation (design ch 11 §8; the PoC's D53).

Markdown files under the state root, never in a project's ``.xo/`` and never
synced: ``<state>/engine/chats/<chat id>/memory/MEMORY.md`` is the index (one
line per memory, ``- [Title](file.md) — hook``) and each memory is a file
beside it, which a person can open. A file's version is a hash of its
content; every write passes ``if_version`` (the version last read, or
``new`` for a file that must not exist yet), so a stale writer is refused
with the current content to merge. The store keeps who wrote each file last
(``memory_versions``), in the transaction that records the call. Files are
capped (100 KB; the index 25 KB and 200 lines) and anything that looks like a
credential is refused.

Blocking file I/O: callers run these in a thread, one write per chat at a
time.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from services.errors import ServiceError
from services.storage import flock
from services.storage.atomic_write import write_text_durable
from services.storage.layout import engine_dir

INDEX = "MEMORY.md"
NEW = "new"
MAX_FILE_BYTES = 100 * 1024
MAX_INDEX_BYTES = 25 * 1024
MAX_INDEX_LINES = 200
VERSION_HEX = 12
_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.md$")
_SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}"
                     r"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{10,})")


class MemoryRefused(ServiceError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(code, message, status=status)


def folder(chat_id: str) -> Path:
    return engine_dir() / "chats" / chat_id / "memory"


def version_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:VERSION_HEX]


def check_path(path: str) -> str:
    if not isinstance(path, str) or not _PATH.match(path):
        raise MemoryRefused("invalid_path", "A memory is one markdown file name, such as build-rules.md or "
                                            "MEMORY.md.")
    return path


def read(chat_id: str, path: str = INDEX) -> dict[str, Any]:
    """``{path, content, version}``; the version of a file not written yet is ``new``."""
    target = folder(chat_id) / check_path(path)
    try:
        content = target.read_text(encoding="utf-8", errors="replace")     # a hand edit may not be UTF-8
    except FileNotFoundError:
        return {"path": path, "content": "", "version": NEW}
    return {"path": path, "content": content, "version": version_of(content)}


def list_files(chat_id: str) -> list[dict[str, Any]]:
    root = folder(chat_id)
    out = []
    for target in sorted(root.glob("*.md")) if root.is_dir() else []:
        data = target.read_bytes()
        out.append({"path": target.name, "bytes": len(data), "version": version_of(data.decode("utf-8", errors="replace"))})
    return out


def write(chat_id: str, path: str, content: str, *, if_version: str) -> dict[str, Any]:
    """Replace one memory file whole, compare-and-set on ``if_version``.
    Raises ``MemoryRefused`` (409 ``version_conflict`` with the current
    content to merge)."""
    check_path(path)
    if not isinstance(content, str):
        raise MemoryRefused("invalid_content", "content must be text.")
    data = content.encode("utf-8")
    limit = MAX_INDEX_BYTES if path == INDEX else MAX_FILE_BYTES
    if len(data) > limit:
        raise MemoryRefused("too_large", f"{path} may hold at most {limit // 1024} KB; keep it shorter.")
    if path == INDEX and len(content.splitlines()) > MAX_INDEX_LINES:
        raise MemoryRefused("index_too_long", f"Keep {INDEX} to one short line per memory, at most "
                                              f"{MAX_INDEX_LINES} lines.")
    if _SECRET.search(content):
        raise MemoryRefused("looks_like_a_secret", "Memory is shared; it never holds credentials or keys. Remove "
                                                   "the secret and write again.")
    target = folder(chat_id) / path
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with flock.locked(target):
        current = read(chat_id, path)
        if if_version != current["version"]:
            raise MemoryRefused("version_conflict", f"{path} changed since you read it (now version "
                                                    f"{current['version']}). Merge with its current content and "
                                                    f"write again:\n{current['content']}", status=409)
        write_text_durable(target, content)
    return {"path": path, "version": version_of(content), "bytes": len(data)}
