"""
Claude-code providers-status adapter.

The Anthropic key and the Claude subscription login are two *mutually exclusive*
ways the CLI can be authenticated, and it only ever resolves to one at a time.
Rather than checking each independently (which lights both tiles at once when an
``ANTHROPIC_API_KEY`` is merely present), we run ``claude auth status`` **once**
and route by the auth method the CLI actually resolved to:

- ``apiKeySource == "ANTHROPIC_API_KEY"``  → the Anthropic **API key** tile.
- logged in by any other method (``claude.ai`` login, oauth token) → the
  **Claude Code** oauth tile.

Crucially the probe runs with the **same environment the chat subprocess uses**
(``Adapter.cli_env()``), not the raw process env. The chat path drops a shadowed
``ANTHROPIC_API_KEY`` when a usable native login is present, so a subscription
login wins; if we probed the raw env instead, the tile would advertise a key the
CLI never actually uses. The tiles therefore reflect what chat will *actually*
resolve to, not everything configured. A recent authentication failure from Remote Control or chat is authoritative:
the tile stays disconnected until a successful authenticated call or login clears
that observed failure.

OpenAI is read from the process environment. OpenRouter is different: it is
configured by merging an ``env`` block into the CLI's own ``settings.json`` (see
``routers/cowork_agent/config.py`` + the manifest's
``providers.openrouter.settings_env``), so its presence is read back from that
settings file, not from the process env.
"""

from __future__ import annotations

import os
from typing import Any

from services.cowork_agent.adapters.claude_code.adapter import ClaudeCodeAdapter
from services.cowork_agent.adapters.claude_code.auth_state import last_auth_failure_reason
from services.cowork_agent.providers_status_lib import (
    build_providers_status,
    claude_auth_status,
)
from services.cowork_agent.registry.agent_registry import get_agent
from services.cowork_agent.registry.agent_settings import read_settings_env


def _openrouter_configured() -> bool:
    """True iff settings.json routes to OpenRouter with a non-empty auth token."""
    env = read_settings_env(get_agent("claude_code").config_file)
    base = (env.get("ANTHROPIC_BASE_URL") or "").lower()
    token = (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
    return "openrouter" in base and bool(token)


async def get_providers_status() -> dict[str, Any]:
    auth = await claude_auth_status(env=ClaudeCodeAdapter({}).cli_env())
    logged_in = bool(auth.get("loggedIn"))
    via_api_key = (auth.get("apiKeySource") or "") == "ANTHROPIC_API_KEY"

    failure_reason = None if via_api_key else last_auth_failure_reason()
    result = await build_providers_status(
        "claude_code",
        anthropic_key_present=lambda: logged_in and via_api_key,
        openai_key_present=lambda: bool((os.environ.get("OPENAI_API_KEY") or "").strip()),
        openrouter_key_present=_openrouter_configured,
        claude_oauth_present=lambda: logged_in and not via_api_key and not failure_reason,
    )
    if failure_reason and "claude_code" in result["oauth"]:
        result["oauth"]["claude_code"]["reason"] = failure_reason
    return result


__all__ = ["get_providers_status"]
