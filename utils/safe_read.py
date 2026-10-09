"""Reading a state file whole without trusting what sits at its path.

A store that calls ``path.read_text()`` on every tick trusts that the path is
a small regular file. A FIFO there makes ``open()`` wait for a writer forever;
a link to ``/dev/zero`` never ends; a runaway file is read whole into memory
each time. On a live server, ``scheduler/jobs.json -> /dev/zero`` allocated
about 1 GB per watcher tick until ``MemoryError``.

:func:`read_text_guarded` refuses all of these before opening anything. Its
errors are ``OSError`` subclasses, so a caller that already handles "can't
read the file" (``except OSError``) handles them the same way. Lives in
``utils/`` because the scheduler sits below ``services/`` and may not import it.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

#: The most a state file is read whole. State files are small; this only
#: stops a runaway file (or a device posing as one) from costing the server
#: its memory. The same bound as xo-doctor's reader.
MAX_STATE_FILE_BYTES = 50 * 1024 * 1024


class NotARegularFile(OSError):
    """The path is a FIFO, socket, device or folder, not a file."""


class FileTooLarge(OSError):
    """The file is larger than a state file is allowed to be."""


def read_text_guarded(path: Path | str, *, max_bytes: int | None = MAX_STATE_FILE_BYTES,
                      encoding: str = "utf-8", errors: str = "strict") -> str:
    """``Path.read_text`` for a state file. A link to a regular file is
    followed, as before. Raises ``FileNotFoundError`` like ``read_text``,
    :class:`NotARegularFile` or :class:`FileTooLarge` without opening the
    path, and ``UnicodeDecodeError`` like ``read_text``. ``max_bytes=None``
    is for append-only logs (timelines, run histories), which grow by design:
    only the kind of file is checked."""
    path = os.fspath(path)
    info = os.stat(path)  # never blocks and never reads
    if not stat.S_ISREG(info.st_mode):
        raise NotARegularFile(errno.EINVAL, "not a regular file", path)
    if max_bytes is not None and info.st_size > max_bytes:
        raise FileTooLarge(errno.EFBIG, f"larger than {max_bytes} bytes", path)
    # O_NONBLOCK and the second check close the gap in which the path could
    # be swapped for a FIFO after the stat; on a regular file both are no-ops.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise NotARegularFile(errno.EINVAL, "not a regular file", path)
        data = handle.read() if max_bytes is None else handle.read(max_bytes + 1)  # it may have grown
    if max_bytes is not None and len(data) > max_bytes:
        raise FileTooLarge(errno.EFBIG, f"larger than {max_bytes} bytes", path)
    return data.decode(encoding, errors)
