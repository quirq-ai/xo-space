"""
Binds an xo-project to the OpenClaw agent its turns run in.

Claude Code runs every turn in the chat's project folder. OpenClaw has no
per-request working directory: an agent's turns run in its entry's ``cwd``
(``agents.entries.<id>.cwd``, OpenClaw ``resolveAgentRunCwd``), which is also
where it reads project context files (``AGENTS.md``) from, while the agent's
persona files stay in its workspace.

So each project runs in the OpenClaw agent named like it: a missing agent is
added with ``openclaw agents add``, and every project agent's ``cwd`` is kept
on the project folder with ``openclaw config patch`` (see ``cli.py``). The
gateway applies ``agents.entries`` changes without a restart, a moment after
the CLI returns; a new chat lets the last write settle and waits until the
gateway serves the agent (``cli.seconds_until_applied``,
``streaming.wait_for_agent``). A chat with no project runs in the configured
default agent, which is left as configured.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from services.cowork_agent.adapters.openclaw import cli
from services.cowork_agent.adapters.openclaw.cli import OpenclawCliError
from services.cowork_agent.adapters.openclaw.paths import DEFAULT_OPENCLAW_WORKSPACE, OPENCLAW_JSON
from services.cowork_agent.adapters.openclaw.store import (
    find_agent_entry_index,
    list_agent_entries,
    load_openclaw_config,
    resolve_agent_workspace_dir,
    resolve_default_agent_id,
    seed_agent_workspace,
)
from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.project_layout import load_project, project_dir

NO_PROJECT = "default"

#: Serialises this process's read-then-write cycles on openclaw.json, so two
#: chats opening in one project add its agent once. The CLI locks the file.
config_lock = threading.RLock()


class BindingError(RuntimeError):
    """The project's OpenClaw agent could not be prepared."""


def agent_id_for_project(project_id: Optional[str]) -> Optional[str]:
    """The agent a project runs in; None for no project (the default agent)."""
    if not project_id or project_id == NO_PROJECT:
        return None
    return normalize_agent_id(project_id)


def add_agent(
    cfg: dict,
    agent_id: str,
    name: str,
    *,
    cwd: Optional[Path] = None,
    workspace: Optional[Path] = None,
) -> None:
    """Add ``agent_id`` with ``openclaw agents add``, then set its display
    name and run directory in one ``config patch``.

    The workspace defaults to ``~/.openclaw/workspace-<id>``. It is seeded from
    the default agent's workspace first, as agent creation always did;
    ``agents add`` keeps those files and adds only the bootstrap files still
    missing. ``agents add`` also names the agent's identity after its id; the
    patch removes that, so the agent keeps the persona it was seeded with.
    Call with :data:`config_lock` held. Raises :class:`cli.OpenclawCliError`.
    """
    aid = normalize_agent_id(agent_id)
    workspace = workspace or resolve_agent_workspace_dir(cfg, aid)
    seed_agent_workspace(workspace, DEFAULT_OPENCLAW_WORKSPACE)
    cli.add_agent(aid, workspace)
    entry: dict = {"identity": None}
    if name and name != aid:
        entry["name"] = name
    if cwd is not None:
        entry["cwd"] = str(cwd)
    cli.patch({"agents": {"entries": {aid: entry}}})


def ensure_project_agent(project_id: Optional[str]) -> str:
    """The OpenClaw agent for ``project_id``, added and pointed at the project
    folder if needed; the default agent for no project. Raises
    :class:`BindingError`."""
    aid = agent_id_for_project(project_id)
    if aid is None:
        # A read only: no need to wait behind another chat's CLI writes.
        return resolve_default_agent_id(load_openclaw_config())
    with config_lock:
        cfg = load_openclaw_config()
        if not OPENCLAW_JSON.is_file():
            raise BindingError(f"{OPENCLAW_JSON} not found; set up OpenClaw before chatting in a project")
        folder = project_dir(project_id)
        # Created like the CLI backends create a turn's working directory.
        folder.mkdir(parents=True, exist_ok=True)
        entries = list_agent_entries(cfg)
        idx = find_agent_entry_index(entries, aid)
        try:
            if idx < 0:
                name = (load_project(project_id) or {}).get("display_name") or aid
                add_agent(cfg, aid, name, cwd=folder)
            elif entries[idx].get("cwd") != str(folder):
                cli.patch({"agents": {"entries": {aid: {"cwd": str(folder)}}}})
        except (OpenclawCliError, OSError) as exc:
            raise BindingError(str(exc)) from exc
    return aid
