"""Composio in a storm session, mounted exactly as today's chat mounts it
(owner §0.108; design ch 10 §4.1).

Today a chat turn gets one extra MCP server, ``composio``, at XO's loopback
proxy (``http://127.0.0.1:<PORT>/mcp/composio-proxy/u/<token>``), when a
Composio key is configured. The token in that URL is the workspace's proxy
token, so the URL is a secret and never goes into a stored spec: a preset
puts only a marker there (the name and transport, no URL), and ``filled``
puts today's URL in when the session's process starts, behind today's gate,
or leaves the server out. The runtime writes it into that process's own MCP
config (0600, in its run folder) and nowhere else. Nothing in Composio
changes: the URL comes from the same call today's per-turn file used.

Core and agent-neutral: every runtime gets the same entry.
"""

from __future__ import annotations

import dataclasses
import logging

from services.cowork_agent.adapters.contract import McpServer, SessionSpec
from services.cowork_agent.connectors.composio import byo_key
from services.cowork_agent.connectors.composio import service as composio_service

log = logging.getLogger(__name__)

#: The server's name. It must match the entry the reconcile sweep installs
#: in each agent's own MCP config: a session's MCP config adds to that one,
#: so another name would list every Composio tool twice.
SERVER_NAME = "composio"


def marker() -> McpServer:
    """What a preset puts in the spec: Composio, with no address yet."""
    return McpServer(name=SERVER_NAME, transport="http")


def _is_marker(server: McpServer) -> bool:
    return server.name == SERVER_NAME and server.transport == "http" and server.url is None


def filled(spec: SessionSpec) -> SessionSpec:
    """The spec a process opens with: the marker becomes today's proxy entry,
    or is left out when no Composio key is configured or the proxy URL can't
    be made (the session runs without Composio tools, as today's turn did).
    Blocking (the proxy token is read, or minted once, under its store's
    lock): call it in a thread."""
    if not any(_is_marker(s) for s in spec.mcp_servers):
        return spec
    url = None
    if not byo_key.configured():
        log.info("storm: no Composio key is configured; this session runs without Composio tools")
    else:
        try:
            url = composio_service._composio_proxy_url()
        except Exception as exc:  # noqa: BLE001 - as today: no Composio tools rather than no session
            log.warning("storm: Composio's proxy URL can't be made (%s); this session runs without its tools",
                        type(exc).__name__)
    servers = tuple(dataclasses.replace(s, url=url) if _is_marker(s) else s
                    for s in spec.mcp_servers if url is not None or not _is_marker(s))
    return dataclasses.replace(spec, mcp_servers=servers)
