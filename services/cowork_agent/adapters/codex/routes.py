"""Mounted only for Codex spaces, following Claude's adapter-owned lifecycle."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from . import remote_control
from routers.browser_guard import is_local_mutation

router = APIRouter(prefix="/api/codex/remote-control")


def _local_control(request: Request):
    # Hosted callers arrive through the local, authenticated Coder proxy.
    if not is_local_mutation(request):
        raise HTTPException(403, "Use the local Space API or its authenticated workspace proxy.")


async def _respond(action):
    try:
        return JSONResponse(await action(), headers={"Cache-Control": "no-store"})
    except remote_control.RemoteControlError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code,
                            headers={"Cache-Control": "no-store"})


@router.get("/status")
async def status():
    return await _respond(remote_control.status)


@router.post("/start", dependencies=[Depends(_local_control)])
async def start():
    return await _respond(remote_control.start)


@router.post("/stop", dependencies=[Depends(_local_control)])
async def stop():
    return await _respond(remote_control.stop)


@router.post("/pair", dependencies=[Depends(_local_control)])
async def pair():
    return await _respond(remote_control.pair)
