"""Advisory file lock helper, used for files written by both the
watcher and the BFF API endpoints.

Two files need it today, for two different reasons:

* ``sessions/sessions-augment.json`` — genuinely two writers. The
  watcher tick writes the message / tool counters; the todos API writes
  the ``taskCount`` ones, on a request thread, now that it is the
  source of todo lifecycle events (syncplan §7, T7).
* ``.xo/todos.json`` — one writer, many callers. The watcher's todo
  sink is gone (T8), so the agent-facing ``POST/PATCH/DELETE /todos``
  endpoints own the file outright; but two concurrent requests (the
  FastAPI thread pool, or a second uvicorn worker) are still two
  read-modify-writes racing for the same document.

Every other ``.xo/`` file is single-writer.

POSIX ``fcntl.flock`` with ``LOCK_EX``. Bounded wait so a wedged
watcher can never block a user-facing API call indefinitely — the
endpoint falls through after the deadline and accepts the small
race (logged WARN).

**Lock files live OUTSIDE ``.xo/``.** They are per-machine
infrastructure (coordination state) — not project data — so they
sit alongside ``offsets.json`` under
``~/.quirq/watcher/locks/``. Keeping them out of ``.xo/`` means:

* agents reading ``.xo/`` never trip over a 0-byte sentinel,
* AGENTS.md's "everything in ``.xo/`` has a schema" contract stays
  clean,
* if ``.xo/`` is ever copied for sync the locks don't tag along.

The lock filename is ``<basename>.<8-hex>.lock`` where the 8-hex
suffix is a stable hash of the absolute data path — so multiple
projects' ``todos.json`` files have distinct locks. The basename
prefix keeps the file recognisable when grepping the locks dir
during debugging.
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
    """Acquire an exclusive advisory lock for the data file at
    ``path``. The lock sentinel itself lives under
    ``~/.quirq/watcher/locks/`` (see module docstring).

    Bounded wait. If the lock can't be acquired in
    :data:`_DEADLINE_S` seconds the context yields anyway (logged
    WARN) — the watcher and the API are both designed for
    non-destructive read-modify-write so the worst case is a single
    lost update that the next tick / call recovers. (For the counters
    that is literally true — the next event re-derives them; for
    ``todos.json`` the loss would be one API call's edit, which is why
    the deadline is generous relative to a write that takes microseconds.)

    The lock is released by the kernel on fd close (i.e. on context
    exit), so a crash mid-block can't wedge the file.
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
