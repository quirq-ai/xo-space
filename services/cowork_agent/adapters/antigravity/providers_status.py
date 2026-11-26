"""
Antigravity (agy) providers-status adapter.

agy uses a **consumer Google OAuth** login (one token file, self-refreshing) and
needs no per-provider API keys — so the only provider tile is ``antigravity``
itself, reported under ``oauth`` and gated on xo.json's
``models.oauth.antigravity.enabled``. Its ``connected`` flag is the same
file-based login check the status/chat paths use.

We compose the response directly rather than via
``providers_status_lib.build_providers_status`` because that shared composer only
knows the ``claude_code``/``codex`` OAuth keys and the anthropic/openai/openrouter
API keys — none of which apply to agy. The response shape
(``{agent, oauth, api_keys}``) is identical, so the frontend is unaffected.
"""
from __future__ import annotations

import json
from typing import Any

from services.cowork_agent.adapters.antigravity.lifecycle.login import logged_in
from services.cowork_agent.project_layout import xo_projects_root
from services.xo_manifest import build_static_manifest

_AGENT = "antigravity"


def _xo_models_section() -> dict[str, Any]:
    """The ``models`` section of xo.json (cascade already applied), or the static
    manifest fallback when the file is missing/unreadable."""
    try:
        path = xo_projects_root() / ".xo" / "xo.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("models"), dict):
                return data["models"]
    except Exception:
        pass
    try:
        return build_static_manifest(_AGENT).get("models", {})
    except Exception:
        return {}


def _oauth_enabled() -> bool:
    models = _xo_models_section()
    oauth = models.get("oauth") if isinstance(models, dict) else None
    node = oauth.get("antigravity") if isinstance(oauth, dict) else None
    return bool(isinstance(node, dict) and node.get("enabled"))


async def get_providers_status() -> dict[str, Any]:
    oauth: dict[str, dict] = {}
    if _oauth_enabled():
        oauth["antigravity"] = {"connected": logged_in()}
    return {"agent": _AGENT, "oauth": oauth, "api_keys": {}}


__all__ = ["get_providers_status"]
