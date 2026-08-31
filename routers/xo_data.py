"""``/xo/*.json`` — the workspace ``.xo`` directory, served.

Space's data lives in one location on disk: ``~/xo-projects/.xo/``. These
routes are the browser's door to that directory, and the URL mirrors the path
one for one — ``GET /xo/space.json`` is ``<XO root>/.xo/space.json``. Nothing
is generated per request and nothing is served out of ``space_ui/data``; the
watcher materialises the files (see visualizer/workspace/views.py) and this
router hands them over.

Deliberately an allowlist, not a static mount of the directory. ``.xo`` also
holds the capability manifest, the session index and the workspace timeline;
exposing the whole folder over HTTP because three files in it are wanted is a
bigger decision than this change needs to make.

A file that is missing, empty or older than ``XO_VIEW_MAX_AGE_S`` is rebuilt
in a worker thread and written back, so the answer a client gets and the file
on disk never diverge — and so the UI still works with the watcher disabled.
"""

import asyncio
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from services.cowork_agent.visualizer.workspace import views

router = APIRouter(prefix="/xo", tags=["xo-data"])

VIEW_MAX_AGE_S = float(os.getenv("XO_VIEW_MAX_AGE_S", "120"))

_UNAVAILABLE = {
    "space": ("space_unavailable", "Could not build the workspace graph."),
    "dashboard": ("dashboard_unavailable", "Could not build the workspace region graph."),
    "sessions": ("sessions_unavailable", "No session telemetry source is currently available."),
}


async def _serve(name: str):
    try:
        payload, _age = views.read(name, max_age_s=VIEW_MAX_AGE_S)
    except Exception as exc:  # unreadable file — fall through to a rebuild
        print(f"⚠️ .xo/{name}.json unreadable ({exc})")
        payload = None

    if payload is None:
        try:
            payload = await asyncio.to_thread(views.build, name)
        except Exception as exc:
            print(f"⚠️ .xo/{name}.json rebuild failed ({exc})")
            payload = None

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
    """The workspace as eight data regions (q1-q8), each carrying data for
    its own visualization (schema 2 — not the space.json node/edge shape)."""
    return await _serve("dashboard")


@router.get("/sessions.json")
async def sessions_json():
    """Session telemetry merged across every runtime that reports it."""
    return await _serve("sessions")
