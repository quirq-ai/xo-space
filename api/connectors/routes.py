"""``GET /api/connectors``: empty-list stub; the real connector surfaces are the subfolders."""

from fastapi import APIRouter

router = APIRouter()


@router.get("")
def list_connectors():
    return []
