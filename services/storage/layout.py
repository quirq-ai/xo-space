"""The folders of the machine-local state root (``~/.quirq/``).

::

    ~/.quirq/
    ├── projects/      one folder per project, named by pid
    ├── inbox/         the Inbox
    ├── connections/   one folder per connection
    ├── scheduler/     saved commands and their run history
    ├── sharing/       shared repos this machine has already seen
    ├── usage/         how far usage has been reported to XO
    ├── settings/      Space-wide choices
    ├── secrets/       credentials
    ├── cache/         safe to delete: rebuilt automatically
    ├── logs/          safe to delete
    ├── quarantine/    moved aside by a person; delete by hand
    └── .locks/        internal

Each folder is named here once, and every store asks for it through these
functions. :func:`migrate_layout` moves files from where earlier releases
kept them; it runs once at server start, before anything reads or writes.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from services.storage.paths import quirq_state_dir
from utils.runtime_env import logs_dir, scheduler_dir  # noqa: F401  (defined below the services layer)

logger = logging.getLogger(__name__)


def projects_dir() -> Path:
    return quirq_state_dir() / "projects"


def sessions_dir() -> Path:
    """``~/.quirq/sessions/``: the index of sessions started with no project
    (they run in the projects root). Per-project indexes live under
    ``projects/<key>/sessions/``; this is the Space's own, one level up, so no
    project key can ever collide with it."""
    return quirq_state_dir() / "sessions"


def inbox_dir() -> Path:
    return quirq_state_dir() / "inbox"


def connections_dir() -> Path:
    return quirq_state_dir() / "connections"


def sharing_dir() -> Path:
    return quirq_state_dir() / "sharing"


def usage_dir() -> Path:
    return quirq_state_dir() / "usage"


def settings_dir() -> Path:
    return quirq_state_dir() / "settings"


def secrets_dir() -> Path:
    return quirq_state_dir() / "secrets"


def cache_dir() -> Path:
    return quirq_state_dir() / "cache"


def locks_dir() -> Path:
    return quirq_state_dir() / ".locks"


def quarantine_dir() -> Path:
    """Data a person moved aside instead of deleting. Nothing reads it; a
    person empties it by hand."""
    return quirq_state_dir() / "quarantine"


def ensure_parent_dir(path: Path) -> None:
    """Create ``path``'s folder. The credentials folder is kept owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent == secrets_dir():
        try:
            path.parent.chmod(0o700)
        except OSError:
            logger.warning("layout: could not make %s owner-only", path.parent)


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


#: Every move, oldest first. A change that moves a file adds its entry here.
#: A move whose old path depends on agent code (the usage watermark is kept
#: per agent) is adopted by its own store instead, so this module never
#: imports above the storage layer.
MOVES: list[Move] = [
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
    Move("the command log",
         _unless_overridden("QUIRQ_COMMAND_LOG_PATH", "commands.log", lambda: logs_dir() / "commands.log"),
         lambda: logs_dir() / "commands.log"),
    Move("the rotated command log",
         _unless_overridden("QUIRQ_COMMAND_LOG_PATH", "commands.log.1", lambda: logs_dir() / "commands.log"),
         lambda: logs_dir() / "commands.log.1"),
    Move("saved command output", _in_state_root("scheduler", "logs"), lambda: logs_dir() / "scheduler"),
]


def migrate_layout(moves: Optional[list[Move]] = None) -> list[str]:
    """Move every file still at an old path to its new one. Idempotent; never raises.

    - Only the old path exists: it is moved (across filesystems too).
    - Both exist: the new one wins and the old one is left where it is, with a
      warning, so nothing is lost.
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
            logger.exception("layout: moving %s failed; it stays at its old path", move.what)
    try:
        if secrets_dir().is_dir():
            secrets_dir().chmod(0o700)
    except OSError:
        logger.warning("layout: could not make %s owner-only", secrets_dir())
    for line in moved:
        logger.info("layout: moved %s", line)
    return moved


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


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
        "layout: %s and %s both exist; using %s and leaving the old copy in place",
        old, new, new,
    )
