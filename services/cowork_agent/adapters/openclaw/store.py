"""
Read access to OpenClaw's on-disk layout: `openclaw.json` plus the per-agent
directories under `~/.openclaw/agents/<id>/`.

Callers get plain dicts and `Path` objects back and stay oblivious to the
underlying JSON shape. Nothing here writes `openclaw.json`: every write goes
through the `openclaw` CLI (`cli.py`), which validates it and keeps the roster
in the form the installed release requires.
"""

import json
import shutil
from pathlib import Path

from services.cowork_agent.adapters.openclaw.paths import (
    DEFAULT_OPENCLAW_WORKSPACE,
    OPENCLAW_DIR,
    OPENCLAW_JSON,
)
from services.cowork_agent.registry.settings import _WORKSPACE_SEED_FILES
from services.cowork_agent.helpers import normalize_agent_id


# ── openclaw.json read ───────────────────────────────────────────────────────


def load_openclaw_config() -> dict:
    if not OPENCLAW_JSON.exists():
        return {}
    try:
        with open(OPENCLAW_JSON) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── Agent roster traversal ───────────────────────────────────────────────────
#
# Current OpenClaw keys agents by id under ``agents.entries``
# (``src/config/types.agents.ts``); the earlier ``agents.list`` array is only
# migrated by OpenClaw itself (the CLI's writes persist the keyed roster). Both
# forms are read.


def list_agent_entries(cfg: dict) -> list[dict]:
    """Every configured agent, as a dict carrying its ``id``."""
    agents = cfg.get("agents")
    if not isinstance(agents, dict):
        return []
    entries = agents.get("entries")
    if isinstance(entries, dict):
        return [
            {**entry, "id": agent_id}
            for agent_id, entry in entries.items()
            if agent_id and isinstance(entry, dict)
        ]
    lst = agents.get("list")
    if not isinstance(lst, list):
        return []
    return [e for e in lst if isinstance(e, dict) and e.get("id")]


def find_agent_entry_index(entries: list[dict], agent_id: str) -> int:
    aid = normalize_agent_id(agent_id)
    for i, e in enumerate(entries):
        if normalize_agent_id(str(e.get("id", ""))) == aid:
            return i
    return -1


def resolve_default_agent_id(cfg: dict) -> str:
    entries = list_agent_entries(cfg)
    if not entries:
        return "main"
    defaults = [e for e in entries if e.get("default") is True]
    chosen = (defaults[0] if defaults else entries[0]).get("id", "main")
    return normalize_agent_id(str(chosen))


def resolve_agent_workspace_dir(cfg: dict, agent_id: str) -> Path:
    """Mirror OpenClaw resolveAgentWorkspaceDir for local disk layout."""
    aid = normalize_agent_id(agent_id)
    entry = next(
        (e for e in list_agent_entries(cfg) if normalize_agent_id(str(e.get("id", ""))) == aid),
        None,
    )
    if entry and isinstance(entry.get("workspace"), str) and entry["workspace"].strip():
        return Path(entry["workspace"]).expanduser().resolve()

    default_id = resolve_default_agent_id(cfg)
    agents_defaults = (cfg.get("agents") or {}).get("defaults") or {}
    fallback = agents_defaults.get("workspace")
    if aid == default_id:
        if isinstance(fallback, str) and fallback.strip():
            return Path(fallback).expanduser().resolve()
        return DEFAULT_OPENCLAW_WORKSPACE.resolve()
    if isinstance(fallback, str) and fallback.strip():
        return (Path(fallback).expanduser().resolve() / aid).resolve()
    return (OPENCLAW_DIR / f"workspace-{aid}").resolve()


def _agent_model_to_display(model_value) -> str | None:
    if model_value is None:
        return None
    if isinstance(model_value, str):
        return model_value
    if isinstance(model_value, dict):
        p = model_value.get("primary")
        if isinstance(p, str):
            return p
    return None


# ── Workspace scaffolding ────────────────────────────────────────────────────


def seed_agent_workspace(workspace_dir: Path, template_dir: Path) -> None:
    """Create ``workspace_dir`` and copy the template's seed files that it
    does not have yet."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if not template_dir.is_dir():
        return
    for fname in _WORKSPACE_SEED_FILES:
        src = template_dir / fname
        dst = workspace_dir / fname
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
