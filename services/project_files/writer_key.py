"""The UI writer key: proof that a save came through the coworker UI's proxy.

Space has no login, and its write guard lets a request with no ``Origin``
through (that is how CLIs and proxies reach it). The coworker UI's proxy
(``serve.py``) strips ``Origin`` before forwarding, so a save from the UI
and one from a script an agent runs would look the same, and the edit log
would credit the agent's write to the person. The two save routes therefore
need ``X-XO-UI-Writer``: a key only the proxy sends, after its own
same-origin check, from a file in XO's state root, which agents' tools treat
as private (reading it is asked about even in auto mode).

**The file**: ``${QUIRQ_STATE_ROOT:-$HOME/.quirq}/secrets/ui-writer.key``,
beside the Space's other credentials; 64 lowercase hex characters and a
newline; the folder 0700, the file 0600, owned by the user running Space. Made at startup when missing, kept across restarts (a save in
flight never meets a new key), and made anew only when the file is missing,
a link, not a regular file, malformed, or open to group or others.

Not a sandbox: agents run as the same user, so a determined script could
read the key, as it could write any file the person can. It puts the save
route at the bar of the protected paths themselves, not below it.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import re
import secrets
import stat
import threading
from pathlib import Path

from services.storage.layout import secrets_dir

log = logging.getLogger(__name__)

HEADER = "X-XO-UI-Writer"
NAME = "ui-writer.key"
_FORMAT = re.compile(r"[0-9a-f]{64}")
_guard = threading.Lock()


def key_path() -> Path:
    return secrets_dir() / NAME


def _read_valid(path: Path) -> tuple[tuple, str] | None:
    """``(identity, key)`` of a well-formed, private key file, else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid() or st.st_size > 256:
            return None
        text = os.read(fd, 256).decode("ascii", errors="replace").strip()
    finally:
        os.close(fd)
    if not _FORMAT.fullmatch(text):
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size), text


def ensure() -> Path:
    """Make sure a valid key file exists; returns its path. Run at startup."""
    path = key_path()
    with _guard:
        if _read_valid(path) is not None:
            return path
        folder = path.parent
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(folder, 0o700)
        tmp = folder / f".{NAME}.{secrets.token_hex(6)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, (secrets.token_hex(32) + "\n").encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        log.info("project files: made the UI writer key at %s", path)
        return path


def current() -> str | None:
    """The key as the file holds it now (a save reads it each time, so a key
    made anew is honoured at once); None when there isn't a valid one."""
    found = _read_valid(key_path())
    return found[1] if found else None


def matches(presented: str | None) -> bool:
    """Whether a request's header holds the key (constant-time)."""
    expected = current()
    if not presented or expected is None:
        return False
    return hmac.compare_digest(presented.strip().encode("ascii", errors="replace"), expected.encode("ascii"))
