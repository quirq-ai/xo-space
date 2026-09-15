"""
Binds an xo-project to the OpenClaw agent its turns run in.

Claude Code runs every turn in the chat's project folder. OpenClaw has no
per-request working directory: an agent's turns run in its entry's ``cwd``
(``agents.entries.<id>.cwd``, OpenClaw ``resolveAgentRunCwd``), which is also
where it reads project context files (``AGENTS.md``) from, while the agent's
persona files stay in its workspace.

So each project runs in the OpenClaw agent named like it: a missing agent is
added to ``openclaw.json`` (with its workspace scaffolded, as agent creation
does), and every project agent's ``cwd`` is kept on the project folder.
``agents.entries`` changes apply without a gateway restart. A chat with no
project runs in the configured default agent, which is left as configured.
A config still using the legacy ``agents.list`` gets no ``cwd`` (see
``store.apply_agent_entry``).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from services.cowork_agent.adapters.openclaw.paths import OPENCLAW_JSON
from services.cowork_agent.adapters.openclaw.store import (
    apply_agent_entry,
    ensure_openclaw_agent_disk,
    find_agent_entry_index,
    list_agent_entries,
    load_openclaw_config,
    resolve_agent_workspace_dir,
    resolve_default_agent_id,
    uses_legacy_list,
    with_agent_entries,
    write_openclaw_config,
)
from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.project_layout import load_project, project_dir

NO_PROJECT = "default"

#: Serialises openclaw.json read-modify-write cycles in this process.
config_lock = threading.RLock()


class BindingError(RuntimeError):
    """The project's OpenClaw agent could not be prepared."""


def agent_id_for_project(project_id: Optional[str]) -> Optional[str]:
    """The agent a project runs in; None for no project (the default agent)."""
    if not project_id or project_id == NO_PROJECT:
        return None
    return normalize_agent_id(project_id)


def ensure_project_agent(project_id: Optional[str]) -> str:
    """The OpenClaw agent for ``project_id``, added and pointed at the project
    folder if needed; the default agent for no project. Raises
    :class:`BindingError`."""
    aid = agent_id_for_project(project_id)
    with config_lock:
        cfg = load_openclaw_config()
        if aid is None:
            return resolve_default_agent_id(cfg)
        if not OPENCLAW_JSON.is_file():
            raise BindingError(f"{OPENCLAW_JSON} not found; set up OpenClaw before chatting in a project")
        folder = project_dir(project_id)
        # Created like the CLI backends create a turn's working directory.
        folder.mkdir(parents=True, exist_ok=True)
        target = str(folder)
        entries = list_agent_entries(cfg)
        idx = find_agent_entry_index(entries, aid)
        try:
            if idx < 0:
                workspace = resolve_agent_workspace_dir(cfg, aid)
                name = (load_project(project_id) or {}).get("display_name") or aid
                write_openclaw_config(apply_agent_entry(cfg, aid, name, workspace, cwd=Path(target)))
                ensure_openclaw_agent_disk(aid, workspace)
            elif not uses_legacy_list(cfg) and entries[idx].get("cwd") != target:
                updated = [dict(e) for e in entries]
                updated[idx]["cwd"] = target
                write_openclaw_config(with_agent_entries(cfg, updated))
        except OSError as exc:
            raise BindingError(f"could not update {OPENCLAW_JSON}: {exc}") from exc
    return aid
