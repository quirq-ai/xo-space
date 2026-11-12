"""Founder authentication for browser mutation routes; agent bearer tokens grant none."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from services import founder_auth


async def require_founder(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    try:
        await asyncio.to_thread(founder_auth.verify, request.cookies.get(founder_auth.COOKIE))
    except founder_auth.CredentialError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code, "message": exc.message}) from None


def protected(router: APIRouter) -> APIRouter:
    guarded = APIRouter()
    guarded.include_router(router, dependencies=[Depends(require_founder)])
    return guarded
