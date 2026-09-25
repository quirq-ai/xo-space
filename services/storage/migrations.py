"""Moving the state root's files from where earlier releases kept them.

:mod:`services.storage.layout` says where every file lives now; this module
says how an older install gets there. :func:`migrate_layout` runs first in the
server lifespan, before anything reads or writes, and on every start: each
step checks the files themselves (old path present, new path free), so it
works from any earlier release, needs no record of what already ran, and does
nothing once everything is in place.

A layout change adds its moves to :data:`MOVES` as one block, below the
earlier blocks, and says in a comment what changed.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from services.storage.layout import (
    cache_dir, inbox_activity_dir, inbox_dir, logs_dir, projects_dir, secrets_dir,
    settings_dir, sharing_dir,
)
from services.storage.paths import quirq_state_dir
from utils.commands import archive_path_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Move:
    """One file or folder that moved. ``old`` answers ``None`` when an
    environment override points the store elsewhere: then nothing moves.
    ``new`` is ``None`` for data rebuilt on the next tick: the old copy is
    removed instead of moved."""

    what: str
    old: Callable[[], Optional[Path]]
    new: Optional[Callable[[], Path]]


def _in_state_root(*parts: str) -> Callable[[], Path]:
    return lambda: quirq_state_dir().joinpath(*parts)


def _unless_overridden(variable: str, old_name: str, new: Callable[[], Path]) -> Callable[[], Optional[Path]]:
    """The old path, unless ``variable`` points the file somewhere other than its new home."""
    def old() -> Optional[Path]:
        configured = (os.getenv(variable, "") or "").strip()
        if configured and Path(configured).expanduser() != new():
            return None
        return quirq_state_dir() / old_name
    return old


def _command_log() -> Path:
    return inbox_activity_dir() / "commands.log"


def _archived(old_name: str) -> Callable[[], Path]:
    """A free archive name for an old command log, stamped with when it was
    last written: every old copy is finished history, so name order stays the
    order its entries were written in."""
    return lambda: archive_path_for(_command_log(), _modified_at(quirq_state_dir() / old_name))


#: Every move, one block per layout change, oldest first. A move whose old
#: path depends on agent code (the usage watermark is kept per agent) is
#: adopted by its own store instead, so this module never imports above the
#: storage layer.
MOVES: list[Move] = [
    # ── The state root gets one folder per subject ────────────────────────
    Move("the Inbox", _in_state_root("inbox.json"), lambda: inbox_dir() / "inbox.json"),
    Move("sharing bookmarks", _in_state_root("project_sharing"), sharing_dir),
    # workspace/ taken apart: the Space timeline is history nothing rebuilds,
    # so it joins the per-project timelines; the views are rebuilt, so cache/.
    Move("the Space timeline", _in_state_root("workspace", "timeline.jsonl"),
         lambda: projects_dir() / "timeline.jsonl"),
    *[Move(f"the Space view {name}", _in_state_root("workspace", name), lambda name=name: cache_dir() / name)
      for name in ("graph.json", "dashboard.json", "sessions.json", "stats.json", "sessions")],
    Move("older Space timeline segments", _in_state_root("workspace"), projects_dir),
    # watcher/ taken apart: reading positions go beside the history they
    # count, so deleting projects/ resets both; the rest is rebuilt.
    Move("the watcher heartbeat", _in_state_root("watcher", "heartbeat.json"),
         lambda: cache_dir() / "heartbeat.json"),
    Move("live presence", _in_state_root("watcher", "activity"), None),
    Move("lock files", _in_state_root("watcher", "locks"), None),
    Move("watcher reading positions", _in_state_root("watcher"), projects_dir),
    # Settings and credentials the state root already held. token.json comes
    # from ~/.config/ through its own store on first read, and uninstall keeps
    # secrets/. The Composio stores stay in ~/.config/composio/.
    Move("saved roots", _in_state_root("roots.env"), lambda: settings_dir() / "roots.env"),
    Move("runtime settings",
         _unless_overridden("QUIRQ_RUNTIME_FILE", "runtime.env", lambda: settings_dir() / "runtime.env"),
         lambda: settings_dir() / "runtime.env"),
    Move("onboarding state", _in_state_root("state.json"), lambda: settings_dir() / "onboarding.json"),
    Move("Space secrets",
         _unless_overridden("QUIRQ_SECRETS_FILE", "secrets.env", lambda: secrets_dir() / "secrets.env"),
         lambda: secrets_dir() / "secrets.env"),
    # Logs the state root already held. The installer's quirq.log is moved by
    # install.sh itself, which holds it open for the server's whole run.
    Move("saved command output", _in_state_root("scheduler", "logs"), lambda: logs_dir() / "scheduler"),

    # ── The command log moves under Inbox → Activity, with its archive ─────
    # It was at the top of the state root, then in logs/, with one rotated
    # .1 generation. Every old copy is finished history, so each goes into
    # the archive and the live log starts fresh: the new file may already
    # hold entries (a launcher runs a command before the server starts), and
    # an archive name is always free, so no copy can find its place taken.
    *[Move(f"the old command log {name}",
           _unless_overridden("QUIRQ_COMMAND_LOG_PATH", name, _command_log),
           _archived(name))
      for name in ("commands.log.1", "commands.log", "logs/commands.log.1", "logs/commands.log")],
]


def migrate_layout(moves: Optional[list[Move]] = None) -> list[str]:
    """Move every file still at an old path to its new one. Idempotent; never raises.

    - Only the old path exists: it is moved (across filesystems too).
    - Both exist: the new one wins and the old one is left where it is, with a
      warning, so nothing is lost. A move whose old copies are history avoids
      this by moving each into an archive name, which is always free.
    - Two folders are merged child by child by the same rules.
    - Rebuilt data (``new`` is ``None``) is removed.

    Returns one line per moved file or folder.
    """
    moved: list[str] = []
    for move in MOVES if moves is None else moves:
        try:
            old = move.old()
            if old is None:
                continue
            if move.new is None:
                _drop(old, moved)
            else:
                _move(old, move.new(), moved)
        except Exception:  # noqa: BLE001 - one bad move must not stop the others
            logger.exception("migrations: moving %s failed; it stays at its old path", move.what)
    try:
        if secrets_dir().is_dir():
            secrets_dir().chmod(0o700)
    except OSError:
        logger.warning("migrations: could not make %s owner-only", secrets_dir())
    for line in moved:
        logger.info("migrations: moved %s", line)
    return moved


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _modified_at(path: Path) -> datetime:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


def _drop(old: Path, moved: list[str]) -> None:
    if not _exists(old):
        return
    if old.is_dir() and not old.is_symlink():
        shutil.rmtree(old)
    else:
        old.unlink()
    moved.append(f"{old} (rebuilt automatically; old copy removed)")


def _move(old: Path, new: Path, moved: list[str]) -> None:
    if not _exists(old) or old == new:
        return
    if not _exists(new):
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
        moved.append(f"{old} -> {new}")
        return
    if old.is_dir() and not old.is_symlink() and new.is_dir() and not new.is_symlink():
        for child in sorted(old.iterdir()):
            _move(child, new / child.name, moved)
        try:
            old.rmdir()
        except OSError:
            pass  # something stayed behind; it was warned about below
        return
    logger.warning(
        "migrations: %s and %s both exist; using %s and leaving the old copy in place",
        old, new, new,
    )
