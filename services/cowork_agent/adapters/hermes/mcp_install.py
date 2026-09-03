"""
``mcp_install`` capability — write the cowork MCP server entry into Hermes'
gateway config.

Hermes exposes MCP via gateway-side config only (unlike claude_code, which
takes a per-session ``--mcp-config``). Core asks for this capability by name
through the loader; the agent literal lives here, in the adapter tree, per the
modularity invariant (DEVELOPING.md §6).

The entry carries only the loopback proxy URL — no headers, no Composio API
key. ``services/cowork_agent/composio/mcp_proxy.py`` injects credentials server-side.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(os.path.expanduser("~/.hermes/config.yaml"))


def install(proxy_url: str) -> dict[str, Any]:
    """Point Hermes' ``mcp_servers.cowork`` at ``proxy_url``.

    Idempotent — re-call to refresh. The caller (the refresh-gateway route) is
    responsible for telling the user to restart Hermes.
    """
    if not CONFIG_PATH.exists():
        return {"ok": False, "error": f"Hermes config not found at {CONFIG_PATH}"}

    try:
        import yaml  # type: ignore
    except ImportError:
        return {"ok": False, "error": "PyYAML not installed; cannot edit Hermes config."}

    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {"ok": False, "error": f"Failed to read Hermes config: {exc}"}

    entry: dict[str, Any] = {
        "url": proxy_url,
        "transport": "streamable-http",
        "enabled": True,
    }

    servers = data.setdefault("mcp_servers", {})
    servers["composio"] = entry
    # "cowork" was the previous server name; pop it so a rename doesn't leave two
    # keys pointing at the same proxy (which would list every tool twice).
    servers.pop("cowork", None)
    servers.pop("xo_composio", None)

    tmp = CONFIG_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    return {"ok": True, "config_path": str(CONFIG_PATH), "restart_required": True}
