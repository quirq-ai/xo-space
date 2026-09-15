"""
Binds an xo-project to the hermes profile its turns run in.

Claude Code runs every turn in the chat's project folder. Hermes has no
per-request working directory: a profile's tools run in the ``terminal.cwd``
from that profile's ``config.yaml``, which the gateway exports as
``TERMINAL_CWD`` at startup and which is also where hermes reads project
context files (``AGENTS.md``) from (hermes-agent ``agent/system_prompt.py``).

So each project runs in the profile named like it. A missing profile is
created by cloning the default profile's ``config.yaml``, ``.env`` and
``SOUL.md`` (``hermes profile create <id> --clone-from default``), so it has
the same model and credentials, and every project profile's ``terminal.cwd``
is kept on the project folder. The profile's pooled gateway is stopped when
that value changes so the next turn starts it with the new directory. A chat
with no project runs in the default profile, which is left as configured.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

import yaml

from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.project_layout import project_dir
from services.cowork_agent.registry.agent_registry import get_agent
from utils.commands import run_sync

logger = logging.getLogger(__name__)

DEFAULT_PROFILE = "default"

# Serialises profile creation and config writes in this process.
_lock = threading.Lock()


class BindingError(RuntimeError):
    """The project's hermes profile could not be prepared."""


def profile_id_for_project(project_id: Optional[str]) -> Optional[str]:
    """The profile a project runs in; None for no project (the default profile)."""
    if not project_id or project_id == DEFAULT_PROFILE:
        return None
    profile = normalize_agent_id(project_id)
    return None if profile == DEFAULT_PROFILE else profile


def profile_dir(profile: str) -> Path:
    return get_agent("hermes").agents_dir / profile


def configured_cwd(profile: str) -> Optional[str]:
    """``terminal.cwd`` from a named profile's ``config.yaml``, or None."""
    try:
        data = yaml.safe_load((profile_dir(profile) / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    terminal = data.get("terminal") if isinstance(data, dict) else None
    cwd = terminal.get("cwd") if isinstance(terminal, dict) else None
    return cwd.strip() if isinstance(cwd, str) and cwd.strip() else None


def _run(argv: list[str], *, home: Optional[Path]) -> None:
    manifest = get_agent("hermes")
    env = dict(os.environ)
    if home is not None:
        env["HERMES_HOME"] = str(home)
    label = " ".join(argv[1:4])
    result = run_sync(
        argv,
        cwd=home if home is not None else manifest.cwd,
        env=env,
        timeout=manifest.cli_timeout_seconds,
        separate_stderr=True,
    )
    if result.ok:
        return
    if result.binary_missing:
        raise BindingError(f"{argv[0]} not found on PATH")
    if result.timed_out:
        raise BindingError(f"{label} timed out")
    detail = (result.stderr or result.output or result.exception or "").strip()[:500]
    raise BindingError(f"{label} exited {result.returncode}: {detail}")


def ensure_project_profile(project_id: Optional[str]) -> Optional[str]:
    """The profile for ``project_id``, created and pointed at the project
    folder if needed; None for no project. Raises :class:`BindingError`."""
    profile = profile_id_for_project(project_id)
    if profile is None:
        return None
    binary = get_agent("hermes").binary
    folder = project_dir(project_id)
    # Created like the CLI backends create a turn's working directory.
    folder.mkdir(parents=True, exist_ok=True)
    target = str(folder)
    with _lock:
        home = profile_dir(profile)
        if not home.is_dir():
            _run([binary, "profile", "create", profile, "--clone-from", DEFAULT_PROFILE], home=None)
            logger.info("hermes: created profile %s for project %s", profile, project_id)
        if configured_cwd(profile) != target:
            _run([binary, "config", "set", "terminal.cwd", target], home=home)
            # The gateway reads terminal.cwd once at startup.
            from services.cowork_agent.adapters.hermes import gateway_pool

            gateway_pool.stop_gateway(profile)
    return profile
