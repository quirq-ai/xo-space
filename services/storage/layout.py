"""The folders of the machine-local state root (``~/.quirq/``).

::

    ~/.quirq/
    ├── projects/      one folder per project, named by pid
    ├── inbox/         the Inbox; activity/ holds the command log
    ├── connections/   one folder per connection
    ├── scheduler/     saved commands and their run history
    ├── sharing/       shared repos this machine has already seen
    ├── usage/         how far usage has been reported to XO
    ├── settings/      Space-wide choices
    ├── secrets/       credentials
    ├── cache/         safe to delete: rebuilt automatically
    ├── logs/          safe to delete
    └── .locks/        internal

Each folder is named here once, and every store asks for it through these
functions. A new store goes under the Space UI section and page that shows it
(``<section>/<page>/``, e.g. ``inbox/activity/``); the older folders above keep
their names until the layout as a whole follows the UI.
How an older install's files get here is :mod:`services.storage.migrations`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from services.storage.paths import quirq_state_dir
from utils.runtime_env import inbox_activity_dir, logs_dir, scheduler_dir  # noqa: F401  (defined below the services layer)

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


def ensure_parent_dir(path: Path) -> None:
    """Create ``path``'s folder. The credentials folder is kept owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent == secrets_dir():
        try:
            path.parent.chmod(0o700)
        except OSError:
            logger.warning("layout: could not make %s owner-only", path.parent)
