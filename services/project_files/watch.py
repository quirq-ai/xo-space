"""The watcher: every change of a project's files, whoever made it, recorded
as it lands (contract §2).

One ``watchfiles.awatch`` per project root that has had a viewer (a Files
route read it, ``touch``) or a running session (``busy``) in the last
``IDLE_S``; it covers the project folder and each of its thread worktrees.
Changes are debounced (``DEBOUNCE_MS``) and folded per path: what is on disk
when the batch is read is what is recorded (``edits.observe``), under each
file's lock, so a save through Files is always recorded first, as the
person's, and the watcher then finds nothing new.

What is skipped is ``address.is_noise``: git's files, XO's records,
build and install folders, Space's temp files. A file the store never saw
that appears with exactly the bytes git holds for it (a new worktree, a
clone, a checkout) is no one's new version (``checkout``): it isn't
recorded. A path reached through a
link (watchfiles follows links) is skipped too: it isn't where the file
is. A folder that appears is read whole; one that vanishes takes the
files the store knew under it with it.

**Who.** ``attributor`` (an agent engine's) names the agent whose turn is
running in a folder holding the path; otherwise the change is the disk's.

**Losing track.** A batch over ``OVERFLOW_PATHS`` paths (a checkout, an
unpacked archive) is not recorded file by file: ``file.rescan`` with
``reason: "overflow"`` tells clients to read their open files again. Each
start of a project's watch (the first, or after a failure) says
``reason: "restart"``: what changed while nothing watched isn't known.
Both name ``worktree: null``: the whole project, its worktrees included.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from services.project_files import checkout, edits, events
from services.project_files.address import is_noise, project_root
from services.project_files.save import file_locks

log = logging.getLogger(__name__)

IDLE_S = 30 * 60
DEBOUNCE_MS = 150
STEP_MS = 50
OVERFLOW_PATHS = 10_000
#: How often idle watches are looked for.
SWEEP_S = 60.0
#: How long a failed watch waits before it starts again (doubling).
RETRY_S = (1.0, 60.0)
#: Paths recorded per lock and fsync.
CHUNK = 500

#: ``attributor(path) -> by`` for a change found at an absolute path, or None.
Attributor = Callable[[Path], Mapping[str, Any] | None]


@dataclass
class _Root:
    project_id: str
    root: Path
    touched: float
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class Watch:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._roots: dict[str, _Root] = {}
        self._sweeper: asyncio.Task | None = None
        self.attributor: Attributor | None = None
        #: ``busy(project_id)``: a session is running in the project.
        self.busy: list[Callable[[str], bool]] = []

    # ── life ─────────────────────────────────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._sweeper = loop.create_task(self._sweep())

    async def stop(self) -> None:
        self._loop = None
        if self._sweeper is not None:
            self._sweeper.cancel()
        for watched in list(self._roots.values()):
            await self._end(watched)
        self._roots.clear()

    def touch(self, project_id: str) -> None:
        """A viewer or a session is at this project: watch it (thread-safe;
        a project that doesn't exist is ignored)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._touch, project_id)
        except RuntimeError:
            pass

    def watching(self, project_id: str) -> bool:
        found = self._roots.get(project_id)
        return found is not None and found.task is not None and not found.task.done()

    def _touch(self, project_id: str) -> None:
        if self._loop is None:
            return
        found = self._roots.get(project_id)
        if found is not None:
            found.touched = time.monotonic()
            if found.task is not None and not found.task.done():
                return
        try:
            pid, root = project_root(project_id)
        except Exception:  # noqa: BLE001 - not a project (any more): nothing to watch
            return
        found = self._roots.get(pid)
        if found is None:
            found = self._roots[pid] = _Root(pid, root, time.monotonic())
        found.touched = time.monotonic()
        if found.task is None or found.task.done():
            found.stop = asyncio.Event()
            found.task = self._loop.create_task(self._run(found))

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_S)
            now = time.monotonic()
            for watched in list(self._roots.values()):
                if now - watched.touched < IDLE_S:
                    continue
                if any(_safely(busy, watched.project_id) for busy in self.busy):
                    watched.touched = now
                    continue
                await self._end(watched)
                self._roots.pop(watched.project_id, None)

    async def _end(self, watched: _Root) -> None:
        watched.stop.set()
        if watched.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(watched.task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                watched.task.cancel()
            except Exception:  # noqa: BLE001 - it ended badly; it has ended
                pass

    # ── one project ──────────────────────────────────────────────────────

    async def _run(self, watched: _Root) -> None:
        from watchfiles import Change, awatch

        delay = RETRY_S[0]
        while not watched.stop.is_set():
            events.publish(events.RESCAN, watched.project_id,
                           {"project_id": watched.project_id, "worktree": None, "reason": "restart"})
            try:
                async for batch in awatch(watched.root, watch_filter=self._filter(watched.root),
                                          debounce=DEBOUNCE_MS, step=STEP_MS, stop_event=watched.stop,
                                          ignore_permission_denied=True, recursive=True):
                    if len(batch) > OVERFLOW_PATHS:
                        events.publish(events.RESCAN, watched.project_id,
                                       {"project_id": watched.project_id, "worktree": None, "reason": "overflow"})
                        continue
                    added = {path for change, path in batch if change == Change.added}
                    paths = sorted({path for _change, path in batch})
                    try:
                        overflow = await asyncio.to_thread(self.process, watched.project_id, watched.root, paths, added)
                    except Exception:  # noqa: BLE001 - one batch it couldn't read must not stop the watch
                        log.exception("project files: couldn't record a batch of changes in %s", watched.project_id)
                        overflow = True
                    if overflow:
                        events.publish(events.RESCAN, watched.project_id,
                                       {"project_id": watched.project_id, "worktree": None, "reason": "overflow"})
                    delay = RETRY_S[0]
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a failed watch starts again, and says so
                log.warning("project files: the watch of %s failed (%s); starting again in %.0fs",
                            watched.project_id, exc, delay)
            try:
                await asyncio.wait_for(watched.stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, RETRY_S[1])

    @staticmethod
    def _filter(root: Path) -> Callable[[Any, str], bool]:
        prefix = str(root) + os.sep

        def wanted(_change: Any, path: str) -> bool:
            return path.startswith(prefix) and not is_noise(path[len(prefix):].replace(os.sep, "/"))
        return wanted

    # ── a batch (in a worker thread) ─────────────────────────────────────

    def process(self, project_id: str, root: Path, paths: list[str], added: set[str]) -> bool:
        """Record one batch; True when it was too large to record file by
        file (the caller says ``file.rescan``)."""
        found: dict[str, bool] = {}                       # real rel -> appeared
        prefix = str(root) + os.sep
        for path in paths:
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix):].replace(os.sep, "/")
            if is_noise(rel) or not _direct(root, rel):
                continue
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                st = None
            except OSError:
                continue
            if st is not None and stat.S_ISDIR(st.st_mode):
                for inner in _files_under(root, rel, OVERFLOW_PATHS - len(found) + 1):
                    found.setdefault(inner, True)
                if len(found) > OVERFLOW_PATHS:
                    return True
                continue
            if st is None:
                for inner in edits.live_under(project_id, rel):
                    found.setdefault(inner, False)       # a folder that went at once
            found[rel] = found.get(rel, False) or path in added
        if len(found) > OVERFLOW_PATHS:
            return True
        items = sorted(found.items())
        repositories: dict[Path, dict[str, set[str]]] = {}      # what git holds, read once per batch
        for start in range(0, len(items), CHUNK):
            self._record(project_id, root, items[start:start + CHUNK], repositories)
        return False

    def _record(self, project_id: str, root: Path, items: list[tuple[str, bool]],
                repositories: dict[Path, dict[str, set[str]]] | None = None) -> None:
        # Hashed before the locks are taken (a large file must not hold up a
        # save), then checked under them: a file that changed since is read
        # again, so what is recorded is what is there once the save before
        # it has been recorded. What git already holds is asked before the
        # locks too (``checkout``: a checkout is no one's new version).
        read = {rel: edits.stamped_content_at(root / rel) for rel, _ in items}
        appeared = {rel: read[rel][0] for rel, added in items if added and read[rel][0] is not None}
        in_git = checkout.held_by_git(root, appeared, repositories) if appeared else set()
        with file_locks([root / rel for rel, _ in items]):
            seen = []
            for rel, added in items:
                after, stamp = read[rel]
                held = rel in in_git
                if stamp is None or _stamp_now(root / rel) != stamp:
                    after, held = edits.content_at(root / rel), False
                seen.append(edits.Seen(rel, after, self._by(root / rel), added=added, in_git=held))
            edits.observe(project_id, seen)

    def _by(self, path: Path) -> Mapping[str, Any]:
        if self.attributor is not None:
            try:
                found = self.attributor(path)
            except Exception:  # noqa: BLE001 - attribution is a best guess; the disk's is safe
                log.exception("project files: the attributor failed for a change")
                found = None
            if found:
                return {**edits.writer(edits.AGENT), **found}
        return edits.writer(edits.DISK)


def _stamp_now(path: Path) -> tuple | None:
    try:
        return edits.file_stamp(os.lstat(path))
    except OSError:
        return None


def _safely(fn: Callable[[str], bool], project_id: str) -> bool:
    try:
        return bool(fn(project_id))
    except Exception:  # noqa: BLE001 - a broken predicate keeps nothing alive
        return False


def _direct(root: Path, rel: str) -> bool:
    """The path's folders are real folders in the project, not links."""
    parent = os.path.dirname(rel)
    return not parent or os.path.realpath(root / parent) == str(root / parent)


def _files_under(root: Path, rel: str, cap: int) -> list[str]:
    out: list[str] = []
    for current, dirnames, filenames in os.walk(root / rel, followlinks=False):
        base = Path(current).relative_to(root).as_posix()
        dirnames[:] = [d for d in dirnames if not is_noise(f"{base}/{d}")]
        for name in filenames:
            inner = f"{base}/{name}"
            if not is_noise(inner):
                out.append(inner)
                if len(out) >= cap:
                    return out
    return out


#: The server's one watch.
watch = Watch()


def touch(project_id: str) -> None:
    watch.touch(project_id)
