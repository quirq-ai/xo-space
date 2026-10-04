"""
Codex providers-status adapter.

OpenAI / Anthropic keys are read from the running process environment — codex has
no dedicated ``.env`` file the way openclaw and hermes do; the CLI inherits
whatever cowork-api was launched with. codex's native login is a ChatGPT OAuth
token in ``$CODEX_HOME/auth.json`` — surfaced by the composer's ``codex`` OAuth
tile via ``codex_oauth_connected()``.

We compose via the shared ``build_providers_status`` (like claude_code) because
codex's ``codex`` OAuth key is already known to that composer — so this adapter
needs ZERO core edits. Only enabled providers (per xo.json) appear:
capabilities.json lights up ``oauth.codex`` + ``api_keys.openai`` and disables
anthropic/openrouter, yielding
``{agent:"codex", oauth:{codex:{connected}}, api_keys:{openai:{connected}}}``.
OpenRouter is disabled in the manifest, so its callable never runs; it is passed
as ``lambda: False`` only to satisfy the composer signature.
"""
from __future__ import annotations

import os
from typing import Any

from services.cowork_agent.providers_status_lib import build_providers_status
from .auth import chatgpt_connected


async def get_providers_status() -> dict[str, Any]:
    result = await build_providers_status(
        "codex",
        anthropic_key_present=lambda: bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
        openai_key_present=lambda: bool((os.environ.get("OPENAI_API_KEY") or "").strip()),
        openrouter_key_present=lambda: False,
    )
    if "codex" in result.get("oauth", {}):
        result["oauth"]["codex"]["connected"] = chatgpt_connected()
    return result


__all__ = ["get_providers_status"]
