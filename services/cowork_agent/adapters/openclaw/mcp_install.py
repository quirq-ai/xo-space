"""
``mcp_install`` capability — write the cowork MCP server entry into OpenClaw's
gateway config.

OpenClaw exposes MCP via gateway-side config only (unlike claude_code, which
takes a per-session ``--mcp-config``). Core asks for this capability by name
through the loader; the agent literal lives here, in the adapter tree, per the
modularity invariant (DEVELOPING.md §6).

The entry carries only the loopback proxy URL — no headers, no Composio API
key. ``services/cowork_agent/composio/mcp_proxy.py`` injects credentials server-side.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.path.expanduser("~/.openclaw/openclaw.json"))


def install(proxy_url: str) -> dict[str, Any]:
    """Point OpenClaw's ``mcp.servers.cowork`` at ``proxy_url``.

    Idempotent — re-call to refresh. The caller (the refresh-gateway route) is
    responsible for telling the user to restart OpenClaw.
    """
    if not CONFIG_PATH.exists():
        return {"ok": False, "error": f"OpenClaw config not found at {CONFIG_PATH}"}

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to read OpenClaw config: {exc}"}
    # A config holding a bare JSON scalar (`null`, a list, a string) would make the
    # setdefault calls below raise out of install() and surface as a 500. Refuse it
    # instead — never silently replace a file we could not understand.
    if not isinstance(data, dict):
        return {"ok": False, "error": f"OpenClaw config at {CONFIG_PATH} is not a JSON object."}

    entry: dict[str, Any] = {
        "url": proxy_url,
        "transport": "streamable-http",
        "enabled": True,
    }

    mcp_section = data.setdefault("mcp", {})
    servers = mcp_section.setdefault("servers", {})
    servers["composio"] = entry
    # "cowork" was the previous server name; pop it so a rename doesn't leave two
    # keys pointing at the same proxy (which would list every tool twice).
    servers.pop("cowork", None)
    servers.pop("xo_composio", None)

    plugins = data.get("plugins") or {}
    entries = plugins.get("entries") or {}
    entries.pop("composio", None)

    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    return {"ok": True, "config_path": str(CONFIG_PATH), "restart_required": True}
