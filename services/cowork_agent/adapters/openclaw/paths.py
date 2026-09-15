"""
OpenClaw on-disk layout + API config constants, sourced from the openclaw
manifest (``get_agent("openclaw")``).

These were historically defined in ``services.cowork_agent.registry.settings`` as
``OPENCLAW_*`` / ``AGENTS_DIR``; they live here now so the agent-specific
resolution sits in the openclaw adapter, not in core. ``settings`` re-exports
the same names for backward compatibility, so existing
``from ...settings import AGENTS_DIR`` consumers keep working unchanged.

They are anchored to the openclaw manifest **specifically** (not the active
agent): every consumer is openclaw-specific and must always see openclaw paths
regardless of ``AGENT_NAME``. Code that needs the *active* agent's manifest
calls ``get_active_agent()``.
"""
from __future__ import annotations

from services.cowork_agent.registry.agent_registry import get_agent

_OPENCLAW = get_agent("openclaw")

# ── On-disk layout ───────────────────────────────────────────────────────────

OPENCLAW_DIR = _OPENCLAW.home_dir
AGENTS_DIR = _OPENCLAW.agents_dir
OPENCLAW_JSON = _OPENCLAW.config_file
DEFAULT_OPENCLAW_WORKSPACE = _OPENCLAW.workspace_dir

# ── API config ───────────────────────────────────────────────────────────────

OPENCLAW_API_URL = _OPENCLAW.api_url
OPENCLAW_GATEWAY_TOKEN = _OPENCLAW.api_token
OPENCLAW_MODEL = _OPENCLAW.api_model
OPENCLAW_MODEL_PREFIX = _OPENCLAW.model_prefix
OPENCLAW_SESSION_HEADER = _OPENCLAW.session_header
OPENCLAW_MODEL_CAPABILITIES: dict = dict(_OPENCLAW.model_capabilities)
