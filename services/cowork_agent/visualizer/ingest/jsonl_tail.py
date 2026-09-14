"""Seek-tail reader with offset persistence.

Tails one ``.jsonl`` file from a saved byte offset. Survives process
restarts via a single ``~/.quirq/projects/offsets.json`` file
shared across all tailed files. Detects inode change (rotation /
truncation) and re-reads from byte 0 in that case.

Design:

* :class:`OffsetStore` is the persistent map ``{abs_path: {offset,
  inode}}``. Loaded lazily; flushed atomically by the watcher main
  loop once per tick.
* :func:`read_new_lines` is the per-file reader. Updates the offset
  in-memory; the caller (or the watcher's tick loop) flushes the
  store afterwards.

Why a single shared file rather than one offset file per jsonl:

* Tens of session logs ⇒ tens of tiny files in ``.quirq`` is
  noisy.
* Atomic rewrites of one small JSON beat dozens of separate file
  syncs.
* One round-trip to load all offsets at startup.

Offsets live under ``~/.quirq/`` (the per-machine
state dir) rather than any ``.xo/`` — they are per-machine, not
per-project, and would NEVER be shipped via a future sync layer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, Optional

from services.cowork_agent.visualizer.state import (
    legacy_watcher_state_dir,
    watcher_state_dir,
)

DEFAULT_OFFSETS_PATH = watcher_state_dir() / "offsets.json"
LEGACY_OFFSETS_PATH = legacy_watcher_state_dir() / "offsets.json"


class OffsetStore:
    """In-memory cache of jsonl read offsets, persisted as JSON.

    Not thread-safe — the watcher runs single-asyncio-loop so the
    only writer is the tick coroutine. Concurrent **readers** (the
    BFF) never touch offsets.
    """

    def __init__(
        self,
        store_path: Path = DEFAULT_OFFSETS_PATH,
        *,
        legacy_store_path: Optional[Path] = None,
    ) -> None:
        self.store_path = store_path
        self.legacy_store_path = (
            LEGACY_OFFSETS_PATH
            if legacy_store_path is None and store_path == DEFAULT_OFFSETS_PATH
            else legacy_store_path
        )
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        source_path = self.store_path
        loaded_legacy = False
        if not source_path.is_file() and self.legacy_store_path is not None:
            source_path = self.legacy_store_path
            loaded_legacy = source_path.is_file()
        if not source_path.is_file():
            return
        try:
            raw = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        offsets = raw.get("offsets") if isinstance(raw, dict) else None
        if isinstance(offsets, dict):
            self._data = {
                str(k): {
                    "offset": int(v.get("offset", 0) or 0),
                    "inode":  int(v.get("inode", 0) or 0),
                }
                for k, v in offsets.items()
                if isinstance(v, dict)
            }
            # The next source flush copies the parsed cursor map into
            # ~/.quirq without ever writing the legacy directory.
            self._dirty = loaded_legacy

    def get(self, path: Path) -> Optional[tuple[int, int]]:
        """Return ``(offset, inode)`` for ``path`` or ``None``."""
        self._load()
        rec = self._data.get(str(path))
        if rec is None:
            return None
        return rec["offset"], rec["inode"]

    def set(self, path: Path, *, offset: int, inode: int) -> None:
        self._load()
        cur = self._data.get(str(path))
        if cur and cur["offset"] == offset and cur["inode"] == inode:
            return  # no-op
        self._data[str(path)] = {"offset": offset, "inode": inode}
        self._dirty = True

    def drop(self, path: Path) -> None:
        """Remove an entry — used when a jsonl file is deleted."""
        self._load()
        if str(path) in self._data:
            del self._data[str(path)]
            self._dirty = True

    def flush(self) -> None:
        """Persist if dirty. Atomic: temp file + ``os.replace``."""
        if not self._dirty:
            return
        # Import here to avoid a circular import in tests.
        from services.cowork_agent.visualizer.atomic_write import write_json_atomic
        write_json_atomic(
            self.store_path,
            # ``schema`` replaced ``version``; the reader only needs ``offsets``.
            {"schema": 1, "offsets": self._data},
        )
        self._dirty = False


def _inode_or_zero(path: Path) -> int:
    try:
        return path.stat().st_ino
    except OSError:
        return 0


def read_new_lines(path: Path, store: OffsetStore) -> Iterator[str]:
    """Yield new full lines from ``path`` since the last persisted
    offset.

    On rotation / truncation (inode changed OR file size < saved
    offset) we re-read from byte 0. The watcher's source layer
    handles re-parsing the prefix that was already consumed (it
    keeps a per-session "first-event seen" guard).

    Updates the in-memory offset on every successful read. The caller
    must call :meth:`OffsetStore.flush` at the end of a tick to
    persist.
    """
    if not path.is_file():
        return

    cur_inode = _inode_or_zero(path)
    saved = store.get(path)

    if saved is None:
        # First sighting — start from 0 so we backfill the whole file.
        offset = 0
    else:
        offset, saved_inode = saved
        try:
            size = path.stat().st_size
        except OSError:
            return
        if cur_inode != saved_inode or size < offset:
            offset = 0  # rotated or truncated

    try:
        with open(path, "rb") as fp:
            fp.seek(offset)
            buf = fp.read()
    except OSError:
        return

    if not buf:
        store.set(path, offset=offset, inode=cur_inode)
        return

    # Process complete lines only — a partial trailing line will be
    # picked up on the next tick once the writer finishes it.
    text = buf.decode("utf-8", errors="replace")
    last_complete = text.rfind("\n")
    if last_complete == -1:
        # No newline in buffer; advance nothing.
        store.set(path, offset=offset, inode=cur_inode)
        return

    consumed = text[: last_complete + 1]
    new_offset = offset + len(consumed.encode("utf-8"))
    store.set(path, offset=new_offset, inode=cur_inode)

    for line in consumed.splitlines():
        line = line.strip()
        if line:
            yield line
