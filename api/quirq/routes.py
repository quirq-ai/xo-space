"""Privacy-aware inspection endpoint for machine-local Quirq state."""

from fastapi import APIRouter

from services.cowork_agent.quirq_catalog import quirq_catalog


router = APIRouter()


@router.get("")
def get_quirq_catalog() -> dict:
    return quirq_catalog()
