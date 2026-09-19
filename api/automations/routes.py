"""``GET /api/automations``: empty-list stub the frontend polls to populate its Automations view."""

from fastapi import APIRouter

router = APIRouter()


@router.get("")
def list_automations():
    return []
