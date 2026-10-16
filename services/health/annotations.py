"""What one run of the server was, kept with its failures.

Written once per boot to ``setup/health/boots/<boot id>.json`` so a record
of a failure can be read with the version and switches of the run it came
from. Never: environment values beyond the ones named here, keys, tokens or
the Space id.
"""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from services.health.recorder import REPO_ROOT, SCHEMA
from services.storage import layout
from utils.commands import run_sync


def version() -> str:
    """``git describe`` of this checkout, else ``XO_SPACE_VERSION`` (an image
    build can set it), else ``"unknown"``. Never raises."""
    if (REPO_ROOT / ".git").exists():
        # Through the one command runner (utils.commands), like every command.
        result = run_sync(["git", "-C", str(REPO_ROOT), "describe", "--tags", "--always", "--dirty"],
                          timeout=2, separate_stderr=True, log_label="health: version")
        if result.ok and result.output.strip():
            return result.output.strip().splitlines()[0]
    return (os.getenv("XO_SPACE_VERSION", "") or "").strip() or "unknown"


def _switch(read: Callable[[], bool]) -> Optional[bool]:
    try:
        return bool(read())
    except Exception:  # noqa: BLE001 - an unreadable switch is unknown, not a failed boot
        return None


def switches() -> dict[str, Optional[bool]]:
    """The background parts this run had on, each read by its owner."""
    def watcher() -> bool:
        # The same parse runtime_config.effective_settings applies; read here
        # without resolving the agent, so a broken agent setup can't hide it.
        from services.cowork_agent.runtime_config import _as_bool
        return _as_bool(os.getenv("QUIRQ_WATCHER_ENABLED"), default=True)

    def scheduler() -> bool:
        from utils.commands.scheduler import scheduler_enabled
        return scheduler_enabled()

    def github() -> bool:
        from services.cowork_agent.github_poller import poller_enabled
        return poller_enabled()

    def connections() -> bool:
        from services.connections.poller import poller_enabled
        return poller_enabled()

    def sharing() -> bool:
        from services.cowork_agent.project_sharing.config import enabled
        return enabled()

    return {name: _switch(read) for name, read in (
        ("watcher", watcher), ("scheduler", scheduler), ("github_poller", github),
        ("connections_poller", connections), ("project_sharing", sharing))}


_HOME = re.compile(r"^/(?:home|Users)/[^/]+")


def _shown(path: str, host_variable: str) -> str:
    """A root as a person sees it (the host path in Docker), with the home
    folder as ``~`` so the record carries no user name."""
    shown = (os.getenv(host_variable, "") or "").strip() or path
    home = str(Path.home())
    if home not in ("", "/") and (shown == home or shown.startswith(home + "/")):
        return "~" + shown[len(home):]
    return _HOME.sub("~", shown)


def collect(boot_id: str, started_at: str) -> dict[str, Any]:
    state = str(layout.quirq_state_dir())
    projects = str(Path((os.getenv("XO_PROJECTS_ROOT", "") or "").strip() or "~/xo-projects").expanduser())
    return {
        "schema": SCHEMA,
        "boot_id": boot_id,
        "started_at": started_at,
        "version": version(),
        "python": platform.python_version(),
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "container": Path("/.dockerenv").exists(),
        "agent": (os.getenv("AGENT_NAME", "") or "").strip() or None,
        "switches": switches(),
        "roots": {"state": _shown(state, "QUIRQ_HOST_STATE_ROOT"),
                  "projects": _shown(projects, "QUIRQ_HOST_PROJECTS_ROOT")},
        "argv0": Path(sys.argv[0]).name if sys.argv and sys.argv[0] else None,
    }
