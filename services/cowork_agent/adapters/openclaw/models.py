"""
OpenClaw model listing (`/api/models`).

One model row per agent entry under ``~/.openclaw/agents/`` so the UI can
target ``<prefix>/<agentId>``. Rows are labelled with the *active* agent's
model prefix and provider name, because other agents with no model store of
their own re-export this listing (each says so in its ``models.py``).
"""

from __future__ import annotations

from services.cowork_agent.registry.agent_registry import get_active_agent
from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.adapters.openclaw.store import list_agent_entries, load_openclaw_config
from services.cowork_agent.adapters.openclaw.paths import AGENTS_DIR, OPENCLAW_MODEL_CAPABILITIES

_AGENT = get_active_agent()


def list_models() -> list[dict]:
    """One model row per agent entry so the UI can target `<prefix>/<agentId>`."""
    cfg = load_openclaw_config()
    entries_by_id = {
        normalize_agent_id(str(e.get("id", ""))): e
        for e in list_agent_entries(cfg)
        if e.get("id")
    }
    models: list[dict] = []
    seen: set[str] = set()
    prefix = _AGENT.model_prefix

    if AGENTS_DIR.exists():
        for agent_dir in sorted(AGENTS_DIR.iterdir()):
            if not agent_dir.is_dir():
                continue
            aid = normalize_agent_id(agent_dir.name)
            seen.add(aid)
            meta = entries_by_id.get(aid, {})
            display = meta.get("name") if isinstance(meta.get("name"), str) else None
            label = (display or "").strip() or aid
            models.append(
                {
                    "id": f"{prefix}/{aid}",
                    "name": label,
                    "provider_id": _AGENT.name,
                    "capabilities": dict(OPENCLAW_MODEL_CAPABILITIES),
                    "pricing": {"prompt": 0, "completion": 0},
                    "metadata": {"openclaw_agent_id": aid},
                }
            )

    if not models:
        models.append(
            {
                "id": f"{prefix}/main",
                "name": "main",
                "provider_id": _AGENT.name,
                "capabilities": dict(OPENCLAW_MODEL_CAPABILITIES),
                "pricing": {"prompt": 0, "completion": 0},
                "metadata": {"openclaw_agent_id": "main"},
            }
        )

    return models
