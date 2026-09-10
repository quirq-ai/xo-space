"""
antigravity agents capability.

Implements the uniform agents contract (same surface every adapter exposes):

  list_agents()      -> list[dict]            # sidebar agents
  create_agent(body) -> dict | JSONResponse   # POST /api/agents
  get_detail(id)     -> dict | None           # None if not ours
  patch(id, body)    -> resp | None           # None if not ours
  delete(id)         -> resp | None           # None if not ours

antigravity agents are project folders under xo-projects (shared across
backends); their record lives in ``<project>/.xo/agent.json``. The core router
forwards here via ``load_capability('agents', …)`` instead of branching on the
backend name. Structurally identical to claude_code's agents capability — only
the ``backend`` tag differs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.responses import JSONResponse

from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.project_layout import (
    project_dir,
    scaffold_project,
    xo_dir,
    xo_projects_root,
)
from services.cowork_agent.visualizer.atomic_write import write_json_atomic

_BACKEND = "antigravity"

# Record schema for ``<project>/.xo/agent.json`` (docs/syncplan.md §5.4 ·
# ``visualizer/schema/agent.schema.json``).
_SCHEMA_ID = "xo/agent.schema.json"
_SCHEMA_VERSION = 1


def _meta_path(agent_id: str) -> Path:
    return xo_dir(agent_id) / "agent.json"


def _load(agent_id: str) -> dict | None:
    path = _meta_path(agent_id)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def _load_owned(agent_id: str) -> dict | None:
    """``_load`` restricted to records this backend owns."""
    meta = _load(agent_id)
    if meta is None:
        return None
    backend = meta.get("backend")
    if isinstance(backend, str) and backend and backend != _BACKEND:
        return None
    return meta


def _write(agent_id: str, data: dict) -> None:
    """Write the record to ``<project>/.xo/agent.json``, atomically."""
    write_json_atomic(_meta_path(agent_id), data)


def _agent_info(agent_id: str, meta: dict) -> dict:
    workspace = str(project_dir(agent_id))
    return {
        "name": agent_id,
        "description": meta.get("description") or meta.get("name") or agent_id,
        "mode": "primary",
        "tools": [],
        "permissions": {"rules": []},
        "system_prompt": None,
        "temperature": None,
        "metadata": {
            "backend": _BACKEND,
            "display_name": meta.get("name") or agent_id,
            "workspace": workspace,
        },
    }


def list_agents() -> list[dict]:
    """Sidebar agents: every xo-project dir that has a ``.xo/agent.json``."""
    agents: list[dict] = []
    projects_root = xo_projects_root()
    if projects_root.exists():
        for d in sorted(projects_root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            meta_path = d / ".xo" / "agent.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                meta = {}
            agents.append(_agent_info(d.name, meta))
    return agents


def create_agent(body) -> dict | JSONResponse:
    """Create an antigravity agent: scaffold the project tree + write the record."""
    display_name = body.name.strip()
    agent_id = normalize_agent_id((body.id or body.name).strip())
    description = (body.description or "").strip()

    if _load(agent_id) is not None:
        return JSONResponse(
            status_code=409,
            content={"detail": f'Antigravity agent "{agent_id}" already exists.'},
        )

    try:
        scaffold_project(agent_id, display_name=display_name, description=description)
        meta = {
            "$schema": _SCHEMA_ID,
            "schema": _SCHEMA_VERSION,
            "id": agent_id,
            "name": display_name,
            "description": description,
            "backend": _BACKEND,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write(agent_id, meta)
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    return _agent_info(agent_id, meta)


def get_detail(agent_id: str) -> dict | None:
    """Full agent snapshot if ``agent_id`` is an antigravity agent, else None."""
    aid = normalize_agent_id(agent_id)
    meta = _load_owned(aid)
    if meta is None:
        return None
    workspace_path = project_dir(aid)
    return {
        "id": aid,
        "display_name": (meta.get("name") or "").strip() or aid,
        "description": meta.get("description") or "",
        "workspace": str(workspace_path),
        "model": None,
        "model_raw": None,
        "identity": {"name": None, "emoji": None, "bio": None},
        "config_entry": {},
        "agents_defaults": {},
        "workspace_files": {},
        "on_disk": {
            "agent_dir": str(workspace_path),
            "models_catalog": None,
            "auth_state": None,
            "auth_profiles": None,
        },
        "sessions": {
            "index_path": str(workspace_path / ".sessions"),
            "count": 0,
            "session_ids": [],
        },
        "openclaw_global_auth": {},
        "backend": _BACKEND,
    }


def patch(agent_id: str, body) -> dict | JSONResponse | None:
    """Patch an antigravity agent's name/description; None if not ours."""
    aid = normalize_agent_id(agent_id)
    if _load_owned(aid) is None:
        return None
    if not body.model_fields_set:
        detail = get_detail(aid)
        return detail if detail else JSONResponse(status_code=404, content={"detail": "Not found"})
    meta = _load_owned(aid) or {}
    if body.name is not None:
        meta["name"] = body.name.strip()
    if body.description is not None:
        meta["description"] = body.description.strip()
    _write(aid, meta)
    detail = get_detail(aid)
    return detail if detail else JSONResponse(status_code=500, content={"detail": "Failed to read agent after update"})


def delete(agent_id: str) -> dict | JSONResponse | None:
    """antigravity has no delete contract today (parity with claude_code)."""
    return None


__all__ = ["list_agents", "create_agent", "get_detail", "patch", "delete"]
