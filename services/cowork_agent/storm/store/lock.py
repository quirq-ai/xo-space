"""The engine lock: one engine per state root, held for the server's life
(ch 07 §9.1).

An exclusive, non-blocking ``flock`` on ``~/.quirq/run/engine.lock``. It fails
closed: if another process holds it, this server runs no engine and the routes
that need one answer 503 ``engine_elsewhere``. Not
``services.storage.flock.locked``, which carries on without the lock after two
seconds. The kernel drops the lock when the holder dies, so a crash never
leaves it stuck.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from services.errors import ServiceError
from services.storage.layout import run_dir


class EngineElsewhere(ServiceError):
    def __init__(self, path: Path, holder: str) -> None:
        super().__init__(
            "engine_elsewhere",
            f"Another XO Space server ({holder}) runs the engine for this state root ({path}). "
            "Stop that server, or use it instead.",
            status=503,
        )


def lock_path() -> Path:
    return run_dir() / "engine.lock"


class EngineLock:
    """Take it with ``EngineLock.acquire()``; it stays held until ``release()``
    or the process ends."""

    def __init__(self, path: Path, fd: int) -> None:
        self.path = path
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, path: Path | None = None) -> "EngineLock":
        path = path or lock_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.pread(fd, 32, 0).decode(errors="replace").strip()
            os.close(fd)
            raise EngineElsewhere(path, f"pid {holder}" if holder.isdigit() else "unknown pid") from None
        except BaseException:
            os.close(fd)
            raise
        # Only for a person reading the file: the lock itself is the flock.
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        return cls(path, fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
