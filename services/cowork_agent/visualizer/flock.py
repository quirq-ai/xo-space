"""
Advisory file lock helper, used for files written by both the watcher and the
BFF API endpoints.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from services.cowork_agent.visualizer.state import watcher_state_dir

logger = logging.getLogger(__name__)

_DEADLINE_S = 2.0
_RETRY_INTERVAL_S = 0.02   # 20 ms — gives ~100 retries per deadline window


def _locks_root() -> Path:
    return watcher_state_dir() / "locks"


def _lock_path_for(data_path: Path) -> Path:
    """Map a data file path to its per-machine lock sentinel path.

    ``~/xo-projects/blackhole/.xo/todos.json``
        → ``~/.quirq/watcher/locks/todos.json.<8hex>.lock``

    The 8-hex suffix is the first 8 chars of ``sha256(abs_path)`` —
    short, stable, collision-free in practice. The data file's
    basename is preserved so a human listing the locks dir can tell
    what each lock guards.
    """
    abs_data = str(data_path.resolve()) if data_path.exists() else str(data_path.absolute())
    digest = hashlib.sha256(abs_data.encode("utf-8")).hexdigest()[:8]
    return _locks_root() / f"{data_path.name}.{digest}.lock"


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """
    Acquire an exclusive advisory lock for the data file at ``path``. The lock
    sentinel itself lives under ``~/.quirq/watcher/locks/`` (see module
    docstring).
    """
    lock_path = _lock_path_for(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        fd = open(lock_path, "a+")
        deadline = time.monotonic() + _DEADLINE_S
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break  # acquired
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    logger.warning(
                        "flock: could not acquire %s within %.1fs; proceeding without lock",
                        lock_path, _DEADLINE_S,
                    )
                    break
                time.sleep(_RETRY_INTERVAL_S)
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fd.close()
