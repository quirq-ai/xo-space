"""MCP adapter for Space's shared read-only tools."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from services import space_tools
from services.errors import ServiceError
from services.space_tools import (
    DOCUMENT_BYTES, TOOL_DESCRIPTIONS, InboxStatus, Offset, PageLimit, ProjectDocument,
    list_projects as _list_projects,
    read_project_document as _read_project_document,
    list_todos as _list_todos,
    list_inbox as _list_inbox,
)


async def _read(operation, *args) -> dict:
    try:
        return await space_tools.read(operation, *args)
    except ServiceError as exc:
        raise ToolError(exc.message) from None


def register_tools(server: MCPServer) -> None:
    annotations = ToolAnnotations(
        read_only_hint=True, destructive_hint=False, open_world_hint=False,
    )
    descriptions = {tool["name"]: tool["description"] for tool in TOOL_DESCRIPTIONS}

    @server.tool(description=descriptions["space_list_projects"], annotations=annotations)
    async def space_list_projects(limit: PageLimit = 50, offset: Offset = 0) -> dict:
        return await _read(_list_projects, limit, offset)

    @server.tool(description=descriptions["space_read_project_document"], annotations=annotations)
    async def space_read_project_document(project_id: str, document: ProjectDocument) -> dict:
        return await _read(_read_project_document, project_id, document)

    @server.tool(description=descriptions["space_list_todos"], annotations=annotations)
    async def space_list_todos(project_id: str, limit: PageLimit = 50) -> dict:
        return await _read(_list_todos, project_id, limit)

    @server.tool(description=descriptions["space_list_inbox"], annotations=annotations)
    async def space_list_inbox(status: InboxStatus = "open", limit: PageLimit = 50) -> dict:
        return await _read(_list_inbox, status, limit)
