"""``/xo/*.json`` — Space's three data payloads, served from disk."""

import asyncio
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from services.cowork_agent.visualizer.workspace import views

router = APIRouter(prefix="/xo", tags=["xo-data"])

VIEW_MAX_AGE_S = float(os.getenv("XO_VIEW_MAX_AGE_S", "120"))

# Keyed by view name — which is the URL's name, and no longer necessarily the
# file's; ``views.view_path`` resolves the file.
_UNAVAILABLE = {
    "space": ("space_unavailable", "Could not build the workspace graph."),
    "dashboard": ("dashboard_unavailable", "Could not build the categorized project graph."),
    "sessions": ("sessions_unavailable", "No session telemetry source is currently available."),
}


async def _serve(name: str):
    try:
        # ``stale_ok``: keep the payload even when it is past the window, so a
        # failed rebuild has something correct to fall back on.
        payload, age = views.read(name, max_age_s=VIEW_MAX_AGE_S, stale_ok=True)
    except Exception as exc:  # unreadable file — fall through to a rebuild
        # Name the file, not a guessed directory: the three views no longer
        # share one.
        print(f"⚠️ {name} view unreadable ({exc})")
        payload, age = None, None

    if payload is None or views.is_stale(age, VIEW_MAX_AGE_S):
        try:
            rebuilt = await asyncio.to_thread(views.build, name)
        except Exception as exc:
            print(f"⚠️ {name} view rebuild failed ({exc})")
            rebuilt = None
        if rebuilt is not None:
            payload = rebuilt
        elif payload is not None:
            # Stale beats absent, and beats a 503 even harder: the walk failed,
            # the file on disk is still a valid graph, and the client wants a
            # graph.
            print(f"⚠️ {name} view rebuild produced nothing; serving age={age}")

    if payload is None:
        code, message = _UNAVAILABLE[name]
        raise HTTPException(status_code=503, detail={"code": code, "message": message})
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/space.json")
async def space_json():
    """The workspace graph: projects, folders, files, ties, git history."""
    return await _serve("space")


@router.get("/dashboard.json")
async def dashboard_json():
    """The graph collapsed into five purpose environments. Same schema as
    space.json, so the browser reuses one renderer."""
    return await _serve("dashboard")


@router.get("/sessions.json")
async def sessions_json():
    """Session telemetry merged across every runtime that reports it."""
    return await _serve("sessions")
