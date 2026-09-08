"""``/xo/*.json`` — Space's three data payloads, served from disk.

Nothing is generated per request and nothing is served out of ``space_ui/data``;
the watcher materialises the files (see visualizer/workspace/views.py) and this
router hands them over.

**The URL is a name, not a path.** It used to mirror the filesystem one for
one, and ``GET /xo/space.json`` used to be ``<XO root>/.xo/space.json``. Since
syncplan T14 it is ``~/.quirq/workspace/graph.json``: the graph is derived
state, and ``<XO root>/.xo/space.json`` is now the durable Space *record*,
which could not survive living in a file two unlocked writers rebuild. The
payload this route returns is unchanged, so every ``space_ui/js`` consumer is
untouched — only the coupling went. ``views.view_path`` is the one place that
knows where a view actually lives.

Deliberately an allowlist, not a static mount of a directory. The workspace
``.xo`` also holds the capability manifest, the session index and the workspace
timeline; exposing a whole folder over HTTP because three files in it are
wanted is a bigger decision than this change needs to make.

A file that is missing, empty or older than ``XO_VIEW_MAX_AGE_S`` is rebuilt
in a worker thread and written back, so the answer a client gets and the file
on disk never diverge — and so the UI still works with the watcher disabled.

**Age is the document's own ``generated_at``, not the file's mtime** (syncplan
T25), and a stale read is no longer thrown away. The rebuild is a full
workspace walk plus a ``git log`` per project plus the Argus scan; when it
fails, the correct-if-old payload already in hand is served rather than turned
into a 503 with an intact file sitting on disk.
"""

import asyncio
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from services.cowork_agent.visualizer.workspace import views

router = APIRouter(prefix="/xo", tags=["xo-data"])

VIEW_MAX_AGE_S = float(os.getenv("XO_VIEW_MAX_AGE_S", "120"))

# Keyed by view name — which is the URL's name, and no longer necessarily
# the file's; ``views.view_path`` resolves the file.
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
    """The workspace graph: projects, folders, files, ties, git history.

    Read from ``~/.quirq/workspace/graph.json``. The URL keeps its name for
    the browser; the Space record at ``<XO root>/.xo/space.json`` is a
    different document and is not served here.
    """
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
