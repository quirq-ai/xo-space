"""A channel's memory: what a coordinator learned that must outlive it.

Coordinators are replaced (``recycle.py``); whatever an incarnation learned
survives only here. The shape is Claude Code's own memory folder:
``MEMORY.md`` is the index (one line per memory, ``- [Title](file.md) — hook``)
and each memory is a markdown file beside it::

    <channel dir>/memory/
    ├── MEMORY.md
    ├── <topic>.md
    └── .versions.json       {path: {version, updated_by_session, updated_at}}

Writes use optimistic concurrency, as Claude Code's memory tools do: every
write passes ``if_version``, the 12-hex version from the last read of that
file, or ``new`` for a file that must not exist yet. A stale version is
refused with the current content, so the writer can merge and retry. Files
are capped at 100 KB and the index at 25 KB / 200 lines; anything that looks
like a credential is refused.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Optional

from services.errors import ServiceError
from services.projects_chat import store
from services.storage import flock
from services.storage.atomic_write import write_json_atomic
from services.storage.reader import read_json

INDEX = "MEMORY.md"
MAX_FILE_BYTES = 100 * 1024
MAX_INDEX_BYTES = 25 * 1024
MAX_INDEX_LINES = 200
_PATH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.md$")
_SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
                     r"|xox[baprs]-[A-Za-z0-9-]{10,})")


class MemoryError_(ServiceError):
    """A memory read or write was refused."""


def _dir(project: Optional[str]) -> Path:
    return store.channel(project, create=True).dir / "memory"


def _version(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _check_path(path: str) -> str:
    if not isinstance(path, str) or not _PATH.match(path):
        raise MemoryError_("invalid_path", "A memory is one markdown file name, e.g. build-rules.md or MEMORY.md.")
    return path


def read(project: Optional[str], path: str = INDEX) -> dict:
    """``{path, content, version}``; ``version`` is ``new`` for a file not written yet."""
    path = _check_path(path)
    target = _dir(project) / path
    try:
        content = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"path": path, "content": "", "version": "new"}
    return {"path": path, "content": content, "version": _version(content)}


def list_files(project: Optional[str]) -> list[dict]:
    folder = _dir(project)
    out = []
    for target in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        text = target.read_text(encoding="utf-8")
        out.append({"path": target.name, "bytes": len(text.encode("utf-8")), "version": _version(text)})
    return out


def write(project: Optional[str], path: str, content: str, *, if_version: str, by_session: str) -> dict:
    """Replace a memory file whole. Raises with the current content on a conflict."""
    path = _check_path(path)
    if not isinstance(content, str):
        raise MemoryError_("invalid_content", "content must be text.")
    data = content.encode("utf-8")
    limit = MAX_INDEX_BYTES if path == INDEX else MAX_FILE_BYTES
    if len(data) > limit:
        raise MemoryError_("too_large", f"{path} may hold at most {limit // 1024} KB.")
    if path == INDEX and len(content.splitlines()) > MAX_INDEX_LINES:
        raise MemoryError_("index_too_long", f"Keep {INDEX} to one short line per memory, at most {MAX_INDEX_LINES}.")
    if _SECRET.search(content):
        raise MemoryError_("looks_like_a_secret", "Memory is shared; it never holds credentials or keys.")
    folder = _dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path
    with flock.locked(target):
        current = read(project, path)
        if if_version != current["version"]:
            raise MemoryError_("version_conflict", f"{path} changed since you read it (now version "
                               f"{current['version']}). Current content:\n{current['content']}", status=409)
        tmp = target.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)
        version = _version(content)
        versions_path = folder / ".versions.json"
        versions = read_json(versions_path) or {"schema": 1, "files": {}}
        versions.setdefault("files", {})[path] = {"version": version, "updated_by_session": by_session,
                                                  "updated_at": store.now_ms_iso()}
        write_json_atomic(versions_path, versions)
    return {"path": path, "version": version, "bytes": len(data)}
