"""
REST routes for the Manus AI connector (API key approach).

Endpoints:
  POST /api/connectors/manus/token       — receive & validate an API key
  GET  /api/connectors/manus/status      — current connection status
  POST /api/connectors/manus/disconnect   — delete stored key
  POST /api/connectors/manus/reconnect    — re-validate stored key
"""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services.cowork_agent.connectors.manus import (
    delete_manus_key,
    get_manus_key,
    get_status,
    save_manus_key,
    validate_key,
)

log = logging.getLogger(__name__)
router = APIRouter()


class TokenBody(BaseModel):
    token: str


@router.post("/api/connectors/manus/token")
async def submit_manus_key(body: TokenBody) -> JSONResponse:
    """Validate a Manus API key, store it, and return the connection status."""
    api_key = body.token.strip()
    if not api_key:
        raise HTTPException(400, detail="API key cannot be empty.")

    result = await validate_key(api_key)

    if result.get("valid"):
        save_manus_key(api_key)
        log.info("Manus connected")
        return JSONResponse({"status": "connected"})
    else:
        return JSONResponse(
            {"status": result["status"], "error": result.get("error", "Validation failed.")},
            status_code=400 if result["status"] == "needs_auth" else 502,
        )


@router.get("/api/connectors/manus/status")
async def manus_status() -> JSONResponse:
    """Return the current Manus connector status."""
    status = await get_status()
    return JSONResponse(status)


@router.post("/api/connectors/manus/disconnect")
async def disconnect_manus() -> JSONResponse:
    delete_manus_key()
    return JSONResponse({"status": "needs_auth"})


@router.post("/api/connectors/manus/reconnect")
async def reconnect_manus() -> JSONResponse:
    api_key = get_manus_key()
    if not api_key:
        return JSONResponse({"status": "needs_auth", "error": "No API key stored."})

    result = await validate_key(api_key)
    if result.get("valid"):
        return JSONResponse({"status": "connected"})
    else:
        return JSONResponse(
            {"status": result["status"], "error": result.get("error", "")},
            status_code=502,
        )
