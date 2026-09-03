"""
Persistent MCP install for claude_code — writes ``mcpServers.cowork`` into
``~/.claude.json`` at *user* scope (the CLI's "available in all your projects").

claude_code also takes a per-session ``--mcp-config`` file (see ``mcp_config.py``),
but that file is unlinked when the turn ends, so a ``claude`` process started any
other way never sees Composio. This capability is the durable half: once written,
every ``claude`` run by this uid picks the server up. ``--mcp-config`` is additive
rather than replacing, and ``proxy_token_for_user`` is stable per principal, so the
per-turn file and this entry resolve to the same URL and coexist.

The entry shape is the CLI's own (``{"type": "http", "url": ...}``) — deliberately
not the ``{"transport": "streamable-http", "enabled": true}`` shape the hermes and
openclaw gateways use.

No credential is written: the URL is a loopback proxy path whose opaque token the
backend resolves, injecting COMPOSIO_API_KEY server-side.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.path.expanduser("~/.claude.json"))

SERVER_NAME = "composio"

# Legacy names from earlier iterations, removed on write so a stale entry can't
# shadow the current one. Mirrors the hermes/openclaw installers. "cowork" is here
# because it WAS the server name: without popping it a renamed install leaves both
# keys pointing at the same proxy, and every tool is listed twice.
_LEGACY_NAMES = ("cowork", "xo_composio")


def install(proxy_url: str) -> dict[str, Any]:
    """Idempotently point claude_code's user-scope MCP config at ``proxy_url``.

    Skips the write when the config already says exactly this — the ``claude`` CLI
    rewrites ``~/.claude.json`` wholesale on exit, so an unnecessary
    read-modify-write is a chance to clobber it (or be clobbered). Same discipline
    as ``remote_control.ensure_gates_seeded`` and ``config/agents/claude_code/setup.sh``.
    """
    if not proxy_url:
        return {"ok": False, "error": "No proxy URL supplied."}
    if not CONFIG_PATH.exists():
        return {"ok": False, "error": f"Claude config not found at {CONFIG_PATH}"}

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Never overwrite a config we could not parse — that would discard the
        # user's real settings (~41 KB of them) to install one MCP server.
        return {"ok": False, "error": f"Failed to read Claude config: {exc}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": f"Claude config at {CONFIG_PATH} is not a JSON object."}

    entry = {"type": "http", "url": proxy_url}

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}

    already_current = servers.get(SERVER_NAME) == entry
    has_legacy = any(name in servers for name in _LEGACY_NAMES)
    if already_current and not has_legacy:
        return {
            "ok": True,
            "config_path": str(CONFIG_PATH),
            "restart_required": False,
            "changed": False,
        }

    servers[SERVER_NAME] = entry
    for name in _LEGACY_NAMES:
        servers.pop(name, None)
    data["mcpServers"] = servers

    tmp = tempfile.NamedTemporaryFile(
        "w", dir=str(CONFIG_PATH.parent), delete=False, suffix=".xo-mcp.tmp"
    )
    try:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, CONFIG_PATH)
    except OSError as exc:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        return {"ok": False, "error": f"Failed to write Claude config: {exc}"}

    return {
        "ok": True,
        "config_path": str(CONFIG_PATH),
        "restart_required": True,
        "changed": True,
    }
