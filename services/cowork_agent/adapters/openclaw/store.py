"""
Read/write access to OpenClaw's on-disk layout: `openclaw.json` plus the
per-agent directories under `~/.openclaw/agents/<id>/`.

Everything that mutates the config or scaffolds agent directories lives here;
callers get plain dicts and `Path` objects back and stay oblivious to the
underlying JSON shape.
"""

import json
import shutil
from pathlib import Path

from services.cowork_agent.adapters.openclaw.paths import (
    AGENTS_DIR,
    DEFAULT_OPENCLAW_WORKSPACE,
    OPENCLAW_DIR,
    OPENCLAW_JSON,
)
from services.cowork_agent.registry.settings import _WORKSPACE_SEED_FILES
from services.cowork_agent.helpers import normalize_agent_id


# ── openclaw.json read/write ─────────────────────────────────────────────────


def load_openclaw_config() -> dict:
    if not OPENCLAW_JSON.exists():
        return {}
    try:
        with open(OPENCLAW_JSON) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_openclaw_config(cfg: dict) -> None:
    OPENCLAW_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = OPENCLAW_JSON.with_suffix(".tmp")
    text = json.dumps(cfg, indent=2) + "\n"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(OPENCLAW_JSON)


# ── Agent roster traversal ───────────────────────────────────────────────────
#
# Current OpenClaw keys agents by id under ``agents.entries``
# (``src/config/types.agents.ts``); the earlier ``agents.list`` array is
# rejected by its config schema and only migrated by ``openclaw doctor``. Both
# forms are read. Writes keep the form the file already uses, and a config
# with no roster yet gets ``agents.entries``.


def uses_legacy_list(cfg: dict) -> bool:
    agents = cfg.get("agents")
    return (
        isinstance(agents, dict)
        and isinstance(agents.get("list"), list)
        and not isinstance(agents.get("entries"), dict)
    )


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


def with_agent_entries(cfg: dict, entries: list[dict]) -> dict:
    """Return ``cfg`` with its roster replaced by ``entries`` (each carrying
    ``id``), written in the roster form the config already uses."""
    agents_block = dict(cfg.get("agents") or {})
    if uses_legacy_list(cfg):
        agents_block["list"] = [dict(e) for e in entries]
    else:
        # ``default`` belongs to the legacy list only; entries reject it.
        agents_block["entries"] = {
            str(e["id"]): {k: v for k, v in e.items() if k not in ("id", "default")}
            for e in entries
        }
    return {**cfg, "agents": agents_block}


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


def apply_agent_entry(
    cfg: dict, agent_id: str, name: str, workspace: Path, *, cwd: Path | None = None
) -> dict:
    """
    Add or update an agent's roster entry like OpenClaw applyAgentConfig (add branch).
    When the roster is empty and the new id is not the default agent, adds the default agent first.

    ``cwd`` is the agent's run directory (``agents.entries.<id>.cwd``, OpenClaw
    ``resolveAgentRunCwd``): its turns' tools run there and the project context
    files (``AGENTS.md``) are read from it, while persona files stay in the
    workspace. It is not written into a legacy ``agents.list`` config, whose
    OpenClaw release may not accept the key.
    """
    aid = normalize_agent_id(agent_id)
    default_id = resolve_default_agent_id(cfg)
    next_list = [dict(e) for e in list_agent_entries(cfg)]
    idx = find_agent_entry_index(next_list, aid)
    next_entry: dict = {"id": aid, "name": name, "workspace": str(workspace)}
    if cwd is not None and not uses_legacy_list(cfg):
        next_entry["cwd"] = str(cwd)
    if idx >= 0:
        next_list[idx] = {**next_list[idx], **next_entry}
    else:
        if len(next_list) == 0 and aid != default_id:
            next_list.append({"id": default_id})
        next_list.append(next_entry)
    return with_agent_entries(cfg, next_list)


# ── Workspace / agent-disk scaffolding ───────────────────────────────────────


def seed_agent_workspace(workspace_dir: Path, template_dir: Path) -> None:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if not template_dir.is_dir():
        return
    for fname in _WORKSPACE_SEED_FILES:
        src = template_dir / fname
        dst = workspace_dir / fname
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)


def ensure_openclaw_agent_disk(agent_id: str, workspace_dir: Path) -> None:
    """Create OpenClaw's ~/.openclaw/agents/<id>/agent/ dir and seed the
    agent's workspace directory.

    OpenClaw creates the agent's session store there itself on first use.
    ``workspace_dir`` (legacy behavior: a dedicated ~/.openclaw/workspace-<id>/
    folder) is created and seeded from the default workspace template.
    """
    aid = normalize_agent_id(agent_id)
    (AGENTS_DIR / aid / "agent").mkdir(parents=True, exist_ok=True)
    tpl = DEFAULT_OPENCLAW_WORKSPACE if DEFAULT_OPENCLAW_WORKSPACE.is_dir() else Path()
    seed_agent_workspace(workspace_dir, tpl)
