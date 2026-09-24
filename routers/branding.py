"""Branding settings for the workspace Setup page."""

import asyncio

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from routers.browser_guard import origin_allowed
from routers.cowork_agent.bff.errors import http_error
from services import branding

router = APIRouter(prefix="/branding")


@router.get("")
async def get_branding():
    try:
        result = await asyncio.to_thread(branding.get_branding)
    except branding.BrandingError as exc:
        raise http_error(exc) from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.put("")
async def save_branding(
    request: Request,
    name: str = Form(...),
    logo: UploadFile | None = File(None),
    remove_logo: bool = Form(False),
):
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail="Branding changes require a same-origin request.")
    try:
        content = await logo.read(branding.MAX_LOGO_BYTES + 1) if logo is not None else None
        result = await asyncio.to_thread(branding.save_branding, name, content, remove_logo)
    except branding.BrandingError as exc:
        raise http_error(exc) from exc
    finally:
        if logo is not None:
            await logo.close()
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/logo")
async def get_logo():
    try:
        content, media_type, version = await asyncio.to_thread(branding.get_logo)
    except branding.BrandingError as exc:
        raise http_error(exc) from exc
    return Response(content, media_type=media_type, headers={
        "Cache-Control": "no-cache",
        "ETag": f'"{version}"',
        "X-Content-Type-Options": "nosniff",
    })
