"""``GET /api/plugins/status``: empty stub for the optional plugins check."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/status")
def plugins_status():
    return {}
