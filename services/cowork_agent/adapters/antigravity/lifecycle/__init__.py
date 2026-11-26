"""agy's lifecycle (the ``lifecycle`` capability): where its CLI is, whether
it is new enough, its login check and its sign-in. Its commands and old
Connect URLs are in ``config/agents/antigravity/manifest.json``."""

from __future__ import annotations

import os
import re
from pathlib import Path

from services.cowork_agent.adapters.antigravity.lifecycle.login import logged_in
from services.cowork_agent.adapters.antigravity.lifecycle.signin import SIGN_IN
from services.cowork_agent.adapters.cli_status import resolve_binary
from services.runtime_lifecycle.contract import LifecycleSpec

CLI_PATH_ENV = "AGY_CLI_PATH"
DEFAULT_BINARY = "agy"
#: The agy XO is tested on: an older one still runs (on print mode), and is
#: said to need ``agy update``.
VERSION_FLOOR = (1, 5, 15)
#: Where agy gets installed, looked at when the server's PATH doesn't have
#: it (a server's PATH is often not the login shell's): the workspace
#: template's ``/opt/agy/bin``, agy's own installer's ``~/.local/bin``, and
#: ``/usr/local/bin``.
INSTALL_PLACES = ("/opt/agy/bin/agy", "~/.local/bin/agy", "/usr/local/bin/agy")


def find_binary() -> str:
    """``AGY_CLI_PATH``, then PATH, then ``INSTALL_PLACES``; else the bare
    name, which fails to start as ``cli_missing``."""
    found = resolve_binary(CLI_PATH_ENV, DEFAULT_BINARY)
    if found != DEFAULT_BINARY:
        return found
    home = os.environ.get("HOME") or str(Path.home())
    for place in INSTALL_PLACES:
        candidate = Path(home + place[1:] if place.startswith("~/") else place)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return DEFAULT_BINARY


def version_tuple(version: str | None) -> tuple[int, ...] | None:
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(n) for n in found.groups()) if found else None


def update_note(cli_version: str | None) -> str | None:
    """Why this agy should be updated, or None: it is older than the floor."""
    found = version_tuple(cli_version)
    if found is None or found >= VERSION_FLOOR:
        return None
    floor = ".".join(str(n) for n in VERSION_FLOOR)
    return f"agy {cli_version} is older than {floor}, the version XO is tested on; update it (agy update)"


SPEC = LifecycleSpec(title="Antigravity", binary=find_binary, logged_in=logged_in, sign_in=SIGN_IN,
                     update_note=update_note)
