"""The kernel's own routes: the modules and what the shell renders.

  GET  /api/modules            every module: manifest, effective switches, task status, warnings
  PUT  /api/modules/{name}     body: a partial switch object, e.g. {"tasks": {"poller": false}} or
                               {"enabled": false}; localhost and same-origin only, like the process
                               controls; applies live (routes gate, tasks stop or start, pages leave
                               navigation) and answers the module's fresh description
  GET  /api/ui                 the tabs and every enabled page spec, for the shell

Everything else the server serves belongs to a module (``modules/*``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from routers.browser_guard import is_local_mutation
from services import modules as registry
from services.errors import NotFound
from services.supervisor import supervisor

router = APIRouter(tags=["kernel"])

_NO_STORE = {"Cache-Control": "no-store"}


def _require_local(request: Request) -> None:
    if not is_local_mutation(request):
        raise HTTPException(status_code=403, detail="module switches require a local client and same-origin browser request")


@router.get("/api/modules")
def list_modules() -> JSONResponse:
    return JSONResponse(registry.describe(), headers=_NO_STORE)


@router.get("/api/modules/{name}")
def get_module(name: str) -> JSONResponse:
    return JSONResponse(registry.describe_one(name), headers=_NO_STORE)


@router.put("/api/modules/{name}", dependencies=[Depends(_require_local)])
async def set_module(name: str, body: Any = Body(...)) -> JSONResponse:
    registry.override(name, body)
    await supervisor.reconcile(registry.tasks())
    return JSONResponse(registry.describe_one(name), headers=_NO_STORE)


@router.get("/api/ui")
def ui() -> JSONResponse:
    return JSONResponse(registry.ui(), headers=_NO_STORE)


_WIDGET_TYPES = {".js": "text/javascript", ".css": "text/css", ".json": "application/json"}


@router.get("/space/modules/{name}/ui/{file}")
def widget(name: str, file: str) -> FileResponse:
    """A module's custom widget (``modules/<name>/ui/<file>``), the one
    folder of a module the browser may load. Nothing else under
    ``modules/`` is served."""
    module = registry.get(name)
    if "/" in file or "\\" in file or file.startswith(".") or Path(file).suffix not in _WIDGET_TYPES:
        raise NotFound("widget_not_found", "No such widget.")
    path = module.path / "ui" / file
    if not path.is_file():
        raise NotFound("widget_not_found", "No such widget.")
    return FileResponse(str(path), media_type=_WIDGET_TYPES[path.suffix],
                        headers={"Cache-Control": "no-cache"})
