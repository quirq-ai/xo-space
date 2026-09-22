"""Theme settings for the workspace Setup page."""

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from routers.browser_guard import origin_allowed
from routers.cowork_agent.bff.errors import ForbidExtra, http_error
from services import theme

router = APIRouter(prefix="/theme")


class ThemeBody(ForbidExtra):
    theme: Literal["space", "quirq", "midnight", "graphite", "linen"]


@router.get("")
async def get_theme():
    try:
        result = await asyncio.to_thread(theme.get_theme)
    except theme.ThemeError as exc:
        raise http_error(exc) from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.put("")
async def save_theme(request: Request, body: ThemeBody):
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail="Theme changes require a same-origin request.")
    try:
        result = await asyncio.to_thread(theme.save_theme, body.theme)
    except theme.ThemeError as exc:
        raise http_error(exc) from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
