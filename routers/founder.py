"""Founder cookie verification and browser pairing. Enrollment is a local operator action."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import Field

from routers.cowork_agent.bff.errors import ForbidExtra
from routers.founder_guard import require_founder
from services import founder_auth

router = APIRouter()
PAGE = Path(__file__).with_name("founder.html")


class BrowserSecret(ForbidExtra):
    secret: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)


@router.get("/founder", response_class=HTMLResponse, include_in_schema=False)
@router.get("/api/founder/connect", response_class=HTMLResponse, include_in_schema=False)
async def pairing_page():
    return HTMLResponse(await asyncio.to_thread(PAGE.read_text, encoding="utf-8"), headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
                                   "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})


@router.get("/founder.js", include_in_schema=False)
@router.get("/api/founder/client.js", include_in_schema=False)
async def pairing_script():
    text = await asyncio.to_thread(PAGE.with_suffix(".js").read_text, encoding="utf-8")
    return Response(text, media_type="text/javascript", headers={"Cache-Control": "no-store"})


@router.post("/api/founder/session")
async def connect_browser(body: BrowserSecret, request: Request):
    try:
        await asyncio.to_thread(founder_auth.verify, body.secret)
    except founder_auth.CredentialError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from None
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    # A trusted HTTPS proxy supplies Origin on browser writes; the global browser guard checks it.
    secure = request.url.scheme == "https" or request.headers.get("origin", "").startswith("https://")
    response.set_cookie(founder_auth.COOKIE, body.secret, httponly=True, secure=secure,
                        samesite="strict", path="/", max_age=365 * 24 * 3600)
    return response
