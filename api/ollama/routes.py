"""``GET /api/ollama/status``: stub reporting no local Ollama."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/status")
def ollama_status():
    return {"binary_installed": False, "running": False}
