"""``GET /api/chat/status``: the engine at a glance (ch 07 §11; ch 17 §2.7):
sessions by state, warm and parked counts, the queue by priority and pool,
each pool's state and reset time, the memory guard's reading, open review
items, wake-to-start and start-to-first-token times. 503 ``engine_elsewhere``
when another server runs the engine."""

from __future__ import annotations

from fastapi import APIRouter

from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import engine
from services.errors import ServiceError

router = APIRouter()


@router.get("/api/chat/status")
async def chat_status() -> dict:
    try:
        return await engine.current().status()
    except ServiceError as exc:
        raise storm_error(exc)
