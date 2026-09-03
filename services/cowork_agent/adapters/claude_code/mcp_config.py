"""
Per-session MCP config injection for the Claude CLI.

We materialize a Composio MCP URL into a JSON file and pass
`--mcp-config <file>` to `claude` so the model can reach Composio's
session-level meta-tools (SEARCH_TOOLS, MULTI_EXECUTE_TOOL,
MANAGE_CONNECTIONS, GET_TOOL_SCHEMAS, REMOTE_WORKBENCH, REMOTE_BASH_TOOL).

The URL written into the file is xo-cowork-api's loopback MCP proxy
(`composio_service._composio_proxy_url(user_id)` →
`http://127.0.0.1:<PORT>/mcp/composio-proxy/u/<token>`). The proxy resolves that
opaque token to this session's user and injects COMPOSIO_API_KEY from .env at
request time. Net: this file holds no Composio credentials — only a localhost
URL scoped to one user.

The file lives under /tmp/xo-cowork/<session>/mcp.json and is unlinked
after the subprocess exits.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_MCP_TMP_ROOT = Path(os.getenv("XO_MCP_TMP_ROOT", "/tmp/xo-cowork"))


def write_session_mcp_config(user_id: Optional[str], session_key: Optional[str]) -> Optional[Path]:
    """Write the cowork MCP server config to a per-session JSON file.

    Returns the path to pass to `claude --mcp-config`, or None when MCP
    is unavailable / unconfigured — including when the turn carried no
    identity, since there is no shared Composio account to fall back to.
    """
    if not user_id:
        log.info(
            "mcp_config: turn has no Composio user (no session bearer); "
            "running without Composio tools."
        )
        return None

    try:
        from services.cowork_agent.connectors.composio import service as composio_service
    except Exception as exc:
        log.debug("mcp_config: composio_service not importable: %s", exc)
        return None

    # Loopback proxy URL — no headers, no Composio credentials on disk. The
    # proxy resolves this session's user from the opaque token and injects
    # x-api-key server-side. See services/cowork_agent/composio/mcp_proxy.py.
    try:
        proxy_url = composio_service._composio_proxy_url(user_id)
    except Exception as exc:
        # No user, or the token store is unwritable. Degrade to "no Composio
        # tools this turn" rather than killing the chat.
        log.warning("mcp_config: cannot build user-scoped proxy URL: %s", exc)
        return None

    server_entry = {"type": "http", "url": proxy_url}

    session_dir = _MCP_TMP_ROOT / (session_key or uuid.uuid4().hex)
    session_dir.mkdir(parents=True, exist_ok=True)
    config_path = session_dir / "mcp.json"
    # Key must match adapters/claude_code/mcp_install.SERVER_NAME: --mcp-config is
    # additive, so a per-turn key that disagreed with the persistent one would
    # register both servers and duplicate every tool.
    payload = {"mcpServers": {"composio": server_entry}}

    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".mcp.", suffix=".json", dir=str(session_dir))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        Path(tmp_path).replace(config_path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise

    return config_path


def cleanup_session_mcp_config(config_path: Optional[Path]) -> None:
    """Best-effort cleanup. The session directory is removed; missing is fine."""
    if not config_path:
        return
    try:
        session_dir = config_path.parent
        if session_dir.exists() and session_dir.is_relative_to(_MCP_TMP_ROOT):
            shutil.rmtree(session_dir, ignore_errors=True)
    except Exception as exc:
        log.debug("mcp_config: cleanup failed for %s: %s", config_path, exc)
