"""``/api/project-sharing``: the sharing relay's own status and check-now.

Per-project sharing routes (``/api/xo-projects/{project_id}/share`` ...) are
in ``api/xo_projects/sharing.py``.
"""

from fastapi import APIRouter

from services.cowork_agent.project_sharing import service

router = APIRouter()


@router.get("/status")
def relay_status() -> dict:
    return service.status_snapshot()


@router.post("/check")
def relay_check() -> dict:
    return service.check_now()
