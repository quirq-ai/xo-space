"""``GET /api/mcp/status``: empty-list stub for the optional MCP integrations check."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/status")
def mcp_status():
    return []
