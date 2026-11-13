"""Clone jobs started by ``POST /api/xo-projects`` with ``"wait": false``.

``GET /api/xo-projects/clones/{clone_id}`` is the job (poll it about once a
second); ``DELETE`` cancels it and removes its staging folder. The shape is
in ``services/project_clones.py``.

This router is mounted after every other ``/api/xo-projects/{project_id}/...``
router (see ``bff/__init__.py``), so a project that happens to be named
``clones`` keeps its own routes.
"""
from fastapi import APIRouter, HTTPException, Request

from routers.browser_guard import origin_allowed
from routers.cowork_agent.bff.project_management import clone_error
from services import project_clones
from services.errors import ServiceError

router = APIRouter()


@router.get("/api/xo-projects/clones/{clone_id}")
async def clone_status(clone_id: str) -> dict:
    try:
        return project_clones.clone_status(clone_id)
    except ServiceError as exc:
        raise clone_error(exc) from exc


@router.delete("/api/xo-projects/clones/{clone_id}")
async def cancel_clone(clone_id: str, request: Request) -> dict:
    # No body to require JSON for; the origin rule still applies to browsers.
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail={"code": "same_origin_required",
                                                     "message": "Manage projects from this Space's Setup page."})
    try:
        return await project_clones.cancel_clone(clone_id)
    except ServiceError as exc:
        raise clone_error(exc) from exc
