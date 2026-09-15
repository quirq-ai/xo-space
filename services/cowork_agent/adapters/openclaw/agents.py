"""
OpenClaw agents capability.

Implements the uniform agents contract (same surface every adapter exposes):

  list_agents()              -> list[dict]
  create_agent(body)         -> dict | JSONResponse
  get_detail(agent_id)       -> dict | None    # None if not ours
  patch(agent_id, body)      -> resp | None     # None if not ours
  delete(agent_id)           -> resp | None     # None if not ours

OpenClaw agents live under ``~/.openclaw/agents/<id>/`` and are listed in
``openclaw.json``, which is read directly and written only through the
``openclaw`` CLI (``cli.py``). The core router forwards here via
``load_capability('agents', …)`` instead of branching on
``backend == "openclaw"``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.responses import JSONResponse

from services.cowork_agent.helpers import (
    _path_must_be_under_home,
    _read_json_file_safe,
    _read_text_limited,
    _redact_secrets_nested,
    _summarize_auth_profiles,
    normalize_agent_id,
)
from services.cowork_agent.adapters.openclaw.store import (
    _agent_model_to_display,
    find_agent_entry_index,
    list_agent_entries,
    load_openclaw_config,
    resolve_agent_workspace_dir,
    seed_agent_workspace,
)
from services.cowork_agent.adapters.openclaw import agent_db
from services.cowork_agent.adapters.openclaw import cli as oc_cli
from services.cowork_agent.adapters.openclaw.paths import AGENTS_DIR, DEFAULT_OPENCLAW_WORKSPACE
from services.cowork_agent.registry.settings import _WORKSPACE_DOC_FILES
from services.cowork_agent.adapters.openclaw.project_binding import add_agent, config_lock
from services.cowork_agent.project_layout import (
    project_dir as xo_project_dir,
    project_dir_exists,
    scaffold_project,
    xo_dir,
    xo_projects_root,
)
from services.cowork_agent.visualizer.atomic_write import write_json_atomic

_BACKEND = "openclaw"

# Record schema for ``<project>/.xo/agent.json`` (docs/syncplan.md §5.4 ·
# ``visualizer/schema/agent.schema.json``), shared with the CLI backends.
_SCHEMA_ID = "xo/agent.schema.json"
_SCHEMA_VERSION = 1


def _meta_path(agent_id: str) -> Path:
    return xo_dir(agent_id) / "agent.json"


def _load(agent_id: str) -> dict | None:
    """The project's agent record (any backend's), or None."""
    path = _meta_path(agent_id)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def _load_owned(agent_id: str) -> dict | None:
    """The project's agent record when it is an openclaw one."""
    meta = _load(agent_id)
    return meta if isinstance(meta, dict) and meta.get("backend") == _BACKEND else None


def _write(agent_id: str, data: dict) -> None:
    """Write the record to ``<project>/.xo/agent.json``, atomically."""
    write_json_atomic(_meta_path(agent_id), data)


def _entry_description(entry: dict) -> str:
    """The agent's ``description`` from its roster entry, or ""."""
    description = entry.get("description")
    return description if isinstance(description, str) else ""


def _agent_info_for_id(cfg: dict, agent_id: str, display_name: str | None, description: str) -> dict:
    """xo-cowork AgentInfo shape; `name` is the OpenClaw agent id so session.directory grouping matches.

    ``workspace`` is the folder the agent's turns run in: its project (the
    entry's ``cwd``) when bound to one, else its OpenClaw workspace."""
    aid = normalize_agent_id(agent_id)
    entry = next(
        (e for e in list_agent_entries(cfg) if normalize_agent_id(str(e.get("id", ""))) == aid),
        {},
    )
    run_cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) and entry["cwd"].strip() else None
    return {
        "name": aid,
        "description": description or display_name or aid,
        "mode": "primary",
        "tools": [],
        "permissions": {"rules": []},
        "system_prompt": None,
        "temperature": None,
        "metadata": {
            "backend": "openclaw",
            "openclaw_id": aid,
            "display_name": display_name or aid,
            "workspace": run_cwd or str(resolve_agent_workspace_dir(cfg, aid)),
        },
    }


def _write_patch(cfg: dict, agent_id: str, body) -> None:
    """Apply an agents PATCH body to the agent's roster entry as one
    ``openclaw config patch``: changed fields set, cleared ones deleted, all or
    nothing. Adds the entry first when the agent has a directory but no entry.
    Call with ``config_lock`` held."""
    aid = normalize_agent_id(agent_id)
    workspace: Path | None = None
    if body.workspace is not None:
        workspace = Path(body.workspace.strip()).expanduser().resolve()
        if not _path_must_be_under_home(workspace):
            raise ValueError("workspace must resolve to a path under your home directory")
    if find_agent_entry_index(list_agent_entries(cfg), aid) < 0:
        add_agent(cfg, aid, aid)
        cfg = load_openclaw_config()
    entries = list_agent_entries(cfg)
    idx = find_agent_entry_index(entries, aid)
    if idx < 0:
        raise RuntimeError("could not resolve agent in openclaw.json")
    current_identity = entries[idx].get("identity")
    current_identity = current_identity if isinstance(current_identity, dict) else {}

    # Merge-patch values: a string sets the field, None deletes it.
    changes: dict = {}
    if body.name is not None:
        changes["name"] = body.name.strip() or aid
    if workspace is not None:
        changes["workspace"] = str(workspace)
    for key, raw in (("model", body.model), ("description", body.description)):
        if raw is not None:
            changes[key] = raw.strip() or None

    identity = {
        key: raw.strip() or None
        for key, raw in (("name", body.identity_name), ("emoji", body.identity_emoji))
        if raw is not None
    }
    if identity:
        remaining = {k: v for k, v in {**current_identity, **identity}.items() if v is not None}
        changes["identity"] = identity if remaining else None

    if changes:
        oc_cli.patch({"agents": {"entries": {aid: changes}}})


# ── Uniform agents contract ───────────────────────────────────────────────────


def list_agents() -> list[dict]:
    """Sidebar agents, listed the way the CLI backends list them: every
    xo-project with an openclaw agent record. OpenClaw agents from before
    project binding (no xo-project of that name) are listed too, so they stay
    reachable."""
    agents: list[dict] = []
    cfg = load_openclaw_config()
    entries = {normalize_agent_id(str(e.get("id", ""))): e for e in list_agent_entries(cfg)}
    listed: set[str] = set()
    root = xo_projects_root()
    if root.exists():
        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            record = _load_owned(d.name)
            if record is None:
                continue
            aid = normalize_agent_id(d.name)
            listed.add(aid)
            agents.append(_agent_info_for_id(cfg, aid, record.get("name") or None, record.get("description") or ""))
    if AGENTS_DIR.exists():
        for d in sorted(AGENTS_DIR.iterdir()):
            if not d.is_dir():
                continue
            aid = normalize_agent_id(d.name)
            if aid in listed or project_dir_exists(d.name):
                continue
            meta = entries.get(aid, {})
            display = meta.get("name") if isinstance(meta.get("name"), str) else None
            agents.append(_agent_info_for_id(cfg, d.name, display, _entry_description(meta)))
    return agents


def create_agent(body) -> dict | JSONResponse:
    """Create an openclaw agent: scaffold its xo-project, add or adopt the
    OpenClaw agent named like it with ``cwd`` on the project folder (its
    persona files stay in its workspace), and write the agent record.

    Like the CLI backends, only an existing agent record is a conflict. The
    project folder or the OpenClaw agent may already exist (another backend's
    project, or an agent an earlier chat in the project added) and is adopted.
    """
    display_name = body.name.strip()
    agent_id = normalize_agent_id((body.id or body.name).strip())
    description = (body.description or "").strip()

    if _load(agent_id) is not None:
        return JSONResponse(status_code=409, content={"detail": f'Agent "{agent_id}" already exists.'})

    requested_workspace: Path | None = None
    if body.workspace and body.workspace.strip():
        requested_workspace = Path(body.workspace.strip()).expanduser().resolve()
        if not _path_must_be_under_home(requested_workspace):
            return JSONResponse(
                status_code=400,
                content={"detail": "workspace must resolve to a path under your home directory."},
            )

    try:
        with config_lock:
            cfg = load_openclaw_config()
            scaffold_project(agent_id, display_name=display_name, description=description)
            cwd = xo_project_dir(agent_id)
            if find_agent_entry_index(list_agent_entries(cfg), agent_id) < 0:
                # The persona workspace: the one asked for, else a dedicated
                # ~/.openclaw/workspace-<id>/ folder, which add_agent seeds.
                add_agent(cfg, agent_id, display_name, cwd=cwd, workspace=requested_workspace)
            else:
                changes = {"name": display_name, "cwd": str(cwd)}
                if requested_workspace is not None:
                    changes["workspace"] = str(requested_workspace)
                oc_cli.patch({"agents": {"entries": {agent_id: changes}}})
                # An entry OpenClaw has not run yet has no agent directory or
                # seeded workspace; give it both, as a new agent gets.
                (AGENTS_DIR / agent_id / "agent").mkdir(parents=True, exist_ok=True)
                seed_agent_workspace(
                    requested_workspace or resolve_agent_workspace_dir(cfg, agent_id), DEFAULT_OPENCLAW_WORKSPACE
                )
            next_cfg = load_openclaw_config()
        _write(agent_id, {
            "$schema": _SCHEMA_ID,
            "schema": _SCHEMA_VERSION,
            "id": agent_id,
            "name": display_name,
            "description": description,
            "backend": _BACKEND,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})

    return _agent_info_for_id(next_cfg, agent_id, display_name, description or display_name)


def get_detail(agent_id: str) -> dict | None:
    """Full openclaw agent snapshot, or None if ``agent_id`` isn't an openclaw agent."""
    aid = normalize_agent_id(agent_id)
    agent_root = AGENTS_DIR / aid
    if not agent_root.is_dir():
        return None

    cfg = load_openclaw_config()
    entries = list_agent_entries(cfg)
    idx = find_agent_entry_index(entries, aid)
    entry = dict(entries[idx]) if idx >= 0 else {}

    display = entry.get("name") if isinstance(entry.get("name"), str) else None
    desc = _entry_description(entry)
    identity_cfg: dict = dict(entry["identity"]) if isinstance(entry.get("identity"), dict) else {}

    ws_path = resolve_agent_workspace_dir(cfg, aid)
    workspace_path_str = str(ws_path)
    workspace_files: dict[str, str | None] = {}
    for fname in _WORKSPACE_DOC_FILES:
        content = _read_text_limited(ws_path / fname)
        if content is not None:
            workspace_files[fname] = content
        elif (ws_path / fname).is_file():
            workspace_files[fname] = ""

    agent_disk = agent_root / "agent"
    models_catalog = _read_json_file_safe(agent_disk / "models.json")
    auth_state = _read_json_file_safe(agent_disk / "auth-state.json")
    auth_profiles_raw = _read_json_file_safe(agent_disk / "auth-profiles.json")
    auth_profiles_safe = None
    if isinstance(auth_profiles_raw, dict):
        auth_profiles_safe = _redact_secrets_nested(auth_profiles_raw)

    sessions_index_path = agent_db.session_store_path(aid)
    seen_ids = {info.session_id for info in agent_db.list_sessions(aid) if info.session_id}
    session_count = len(seen_ids)
    session_ids = sorted(seen_ids)[:80]

    global_auth = (cfg.get("auth") or {}).get("profiles")
    global_auth_summary = _summarize_auth_profiles(global_auth) if isinstance(global_auth, dict) else {}

    agents_defaults = cfg.get("agents", {}).get("defaults")
    if not isinstance(agents_defaults, dict):
        agents_defaults = {}

    return {
        "id": aid,
        "display_name": ((display or "").strip() or aid),
        "description": desc,
        "workspace": workspace_path_str,
        "model": _agent_model_to_display(entry.get("model")),
        "model_raw": entry.get("model"),
        "identity": {
            "name": identity_cfg.get("name") if isinstance(identity_cfg.get("name"), str) else None,
            "emoji": identity_cfg.get("emoji") if isinstance(identity_cfg.get("emoji"), str) else None,
            "bio": desc or None,
        },
        "config_entry": entry,
        "agents_defaults": agents_defaults,
        "workspace_files": workspace_files,
        "on_disk": {
            "agent_dir": str(agent_disk.resolve()),
            "models_catalog": models_catalog,
            "auth_state": auth_state,
            "auth_profiles": auth_profiles_safe,
        },
        "sessions": {
            "index_path": str(sessions_index_path.resolve()),
            "count": session_count,
            "session_ids": session_ids,
        },
        "openclaw_global_auth": global_auth_summary,
        "backend": "openclaw",
    }


def patch(agent_id: str, body) -> dict | JSONResponse | None:
    """Patch an openclaw agent's roster entry through the openclaw CLI; None if not ours."""
    aid = normalize_agent_id(agent_id)
    if not (AGENTS_DIR / aid).is_dir():
        return None
    if not body.model_fields_set:
        detail = get_detail(aid)
        return detail if detail else JSONResponse(status_code=404, content={"detail": "Not found"})
    try:
        with config_lock:
            _write_patch(load_openclaw_config(), aid, body)
        record = _load_owned(aid)
        if record is not None and (body.name is not None or body.description is not None):
            if body.name is not None:
                record["name"] = body.name.strip() or aid
            if body.description is not None:
                record["description"] = body.description.strip()
            _write(aid, record)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": str(e)})
    detail = get_detail(aid)
    return detail if detail else JSONResponse(status_code=500, content={"detail": "Failed to read agent after update"})


def delete(agent_id: str) -> dict | JSONResponse | None:
    """openclaw has no delete contract today."""
    return None
