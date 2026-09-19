"""Bounded read-only host skills and transport to the installed JavaScript core."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
import time
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass

from utils.commands import run

NODE_DIR = Path(__file__).with_name("node")
MAX_FILES = 64
MAX_FILE_BYTES = 256_000
MAX_WORKSPACE_BYTES = 4_000_000
MAX_MESSAGE_BYTES = 24_000_000
MAX_SCAN_ENTRIES = 4096
MAX_SCAN_SECONDS = 3.0
MAX_DEPTH = 16
NODE_TIMEOUT = 15.0
_NODE_SLOTS = threading.BoundedSemaphore(2)
_SECRET_NAME = re.compile(r"(?:^|[._-])(?:secrets?|credentials?|tokens?|passwords?|api[_-]?keys?|keyring|oauth)(?:[._-]|$)", re.I)
_EXCLUDED_DIRS = frozenset({"secrets", "credentials", "private_keys", "node_modules", "venv", "__pycache__"})


class FlyRuntimeError(ValueError):
    """The artifact, selected workspace, or structured task is invalid."""


@asynccontextmanager
async def _node_slot():
    # Model listing may run this function through asyncio.run in a worker
    # thread. An asyncio semaphore shared by those loops is not safe.
    started = time.monotonic()
    while not _NODE_SLOTS.acquire(blocking=False):
        if time.monotonic() - started >= NODE_TIMEOUT:
            raise RuntimeError("Fly policy core is busy; retry after the current runs finish.")
        await asyncio.sleep(0.02)
    try:
        yield
    finally:
        _NODE_SLOTS.release()


async def node_call(payload: dict) -> dict:
    """Run only our pinned module; no executable material comes from a fly.

    The shared command executor journals stdout. A private temporary result
    file keeps record values out of that journal; it is removed on every exit.
    Caller cancellation waits for the bounded child to exit before cleanup.
    """
    binary = shutil.which(os.environ.get("FLY_NODE_PATH", "node"))
    if not binary:
        raise RuntimeError("Fly requires Node.js 20 or newer; install Node or set FLY_NODE_PATH.")
    async with _node_slot():
        with tempfile.TemporaryDirectory(prefix="xo-fly-bridge-") as temporary:
            result_path = Path(temporary) / "result.json"
            result_path.touch(mode=0o600)
            expected = result_path.stat()
            envelope = {"request": payload, "outputPath": str(result_path)}
            try:
                encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                raise FlyRuntimeError("Fly request must be finite, valid UTF-8 JSON.") from exc
            if len(encoded) > MAX_MESSAGE_BYTES:
                raise FlyRuntimeError("Fly bridge request exceeds 24 MB.")
            # In particular, inherited NODE_OPTIONS must not preload code into
            # the interpreter used to validate artifacts and read observations.
            env = {key: value for key, value in os.environ.items() if key not in {"NODE_OPTIONS", "NODE_PATH"}}
            command = asyncio.create_task(run(
                [binary, str(NODE_DIR / "bridge.mjs")], input=encoded,
                cwd=NODE_DIR, timeout=NODE_TIMEOUT, separate_stderr=True,
                env=env, log_label="Fly installed policy core",
            ))
            try:
                outcome = await asyncio.shield(command)
            except asyncio.CancelledError:
                # utils.commands owns the process. Do not cancel communicate()
                # and orphan a child; its fixed timeout bounds this wait.
                while not command.done():
                    try:
                        await asyncio.shield(command)
                    except asyncio.CancelledError:
                        continue
                command.result()
                raise
            if not outcome.ok:
                if outcome.timed_out:
                    raise RuntimeError("Fly policy core exceeded its 15-second execution limit.")
                raise RuntimeError("Installed Fly policy core failed; check Node.js and the pinned runtime files.")
            fd = os.open(result_path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                found = os.fstat(fd)
                if (found.st_dev, found.st_ino) != (expected.st_dev, expected.st_ino) or not stat.S_ISREG(found.st_mode) or found.st_nlink != 1:
                    raise RuntimeError("Fly bridge result file changed unexpectedly.")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    body = stream.read(MAX_MESSAGE_BYTES + 1)
            finally:
                os.close(fd)
            if len(body) > MAX_MESSAGE_BYTES:
                raise RuntimeError("Fly bridge result exceeds 24 MB.")
            try:
                result = json.loads(body)
            except (ValueError, UnicodeError) as exc:
                raise RuntimeError("Fly policy core returned an invalid result.") from exc
            if not isinstance(result, dict) or result.get("ok") is not True:
                message = result.get("error", "Invalid Fly artifact or task.") if isinstance(result, dict) else "Invalid Fly result."
                raise FlyRuntimeError(str(message)[:500])
            if not isinstance(result.get("value"), dict):
                raise RuntimeError("Fly policy core returned an invalid result shape.")
            return result["value"]


def _fingerprint(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(frozen=True)
class FileEntry:
    path: str
    parts: tuple[str, ...]
    fingerprint: tuple

    @property
    def size(self) -> int:
        return self.fingerprint[2]


class WorkspaceReader:
    """Own one root directory descriptor; never follow links under that root."""

    def __init__(self, workspace: Path, *, expected_identity: list[int] | None = None):
        self.path = Path(workspace).expanduser()
        if not self.path.is_absolute() or self.path.is_symlink():
            raise FlyRuntimeError("Select an existing absolute workspace directory, not a symbolic link.")
        try:
            self.path = self.path.resolve(strict=True)
            self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise FlyRuntimeError("The selected workspace directory is unavailable.") from exc
        info = os.fstat(self.fd)
        if expected_identity is not None and [info.st_dev, info.st_ino] != expected_identity:
            os.close(self.fd)
            raise FlyRuntimeError("The pinned workspace directory changed. Start a new session.")
        self.entries: dict[str, FileEntry] = {}
        self.excluded = 0
        self.scanned = 0

    def close(self) -> None:
        os.close(self.fd)

    def discover(self) -> dict:
        """Read directory entries and stat metadata only, never file contents."""
        started = time.monotonic()
        total = 0
        normalized: set[str] = set()

        def walk(directory_fd: int, parts: tuple[str, ...]) -> None:
            nonlocal total
            with os.scandir(directory_fd) as iterator:
                for child in iterator:
                    self.scanned += 1
                    if self.scanned > MAX_SCAN_ENTRIES or time.monotonic() - started > MAX_SCAN_SECONDS:
                        raise FlyRuntimeError("Workspace discovery exceeded its bounded scan. Choose a smaller project or data folder.")
                    name = child.name
                    if name.startswith(".") or name.lower() in _EXCLUDED_DIRS or _SECRET_NAME.search(name):
                        self.excluded += 1
                        continue
                    try:
                        info = child.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            self.excluded += 1
                            continue
                        child_parts = (*parts, name)
                        if stat.S_ISDIR(info.st_mode):
                            if len(child_parts) > MAX_DEPTH:
                                raise FlyRuntimeError("Workspace exceeds the 16-directory discovery depth; choose a narrower folder.")
                            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                            try:
                                if _fingerprint(os.fstat(next_fd)) != _fingerprint(info):
                                    raise FlyRuntimeError("A workspace directory changed during discovery; retry the run.")
                                walk(next_fd, child_parts)
                            finally:
                                os.close(next_fd)
                            continue
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or Path(name).suffix.lower() not in {".json", ".csv"}:
                            self.excluded += 1
                            continue
                        relative = "/".join(child_parts)
                        path = unicodedata.normalize("NFC", relative)
                        if len(path) > 240 or any(char in path for char in "\\:") or any(ord(char) < 32 for char in path):
                            raise FlyRuntimeError("Workspace has an unsupported JSON/CSV path; use safe relative file names up to 240 characters.")
                        if path.lower() in normalized:
                            raise FlyRuntimeError("Workspace contains JSON/CSV paths that collide after case or Unicode normalization.")
                        normalized.add(path.lower())
                        if info.st_size > MAX_FILE_BYTES:
                            raise FlyRuntimeError(f"File exceeds the 256 KB limit: {path}")
                        total += info.st_size
                        if len(self.entries) >= MAX_FILES or total > MAX_WORKSPACE_BYTES:
                            raise FlyRuntimeError("Workspace exceeds 64 JSON/CSV files or 4 MB. Choose a smaller data folder; discovery was not silently truncated.")
                        self.entries[path] = FileEntry(path, child_parts, _fingerprint(info))
                    except FlyRuntimeError:
                        raise
                    except OSError as exc:
                        raise FlyRuntimeError("A workspace entry could not be safely inspected; fix permissions or retry after changes stop.") from exc

        walk(self.fd, ())
        name = self.path.name[:160] or "Workspace"
        return {"id": "workspace-" + hashlib.sha256(str(self.path).encode()).hexdigest()[:20], "name": name,
                "files": [{"path": item.path, "bytes": item.size} for item in self.entries.values()]}

    def read(self, relative_path: str) -> dict:
        """Open only a learned choice, relative to held descriptors, checking races."""
        entry = self.entries.get(relative_path)
        if entry is None:
            raise FlyRuntimeError("Policy selected a path outside the discovered workspace.")
        directory_fd = os.dup(self.fd)
        file_fd = None
        try:
            for part in entry.parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(entry.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _fingerprint(before) != entry.fingerprint:
                raise FlyRuntimeError("File changed after discovery or is no longer an eligible regular file.")
            with os.fdopen(file_fd, "rb", closefd=False) as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            if _fingerprint(os.fstat(file_fd)) != entry.fingerprint or len(content) != entry.size:
                raise FlyRuntimeError("File changed while it was being read; retry after writes stop.")
            return {"path": relative_path, "content": content.decode("utf-8", errors="strict")}
        except UnicodeError:
            return {"path": relative_path, "error": "File is not valid UTF-8 text."}
        except (OSError, FlyRuntimeError) as exc:
            message = str(exc) if isinstance(exc, FlyRuntimeError) else "File could not be read safely (missing, changed, linked, or permission denied)."
            return {"path": relative_path, "error": message}
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(directory_fd)
