"""The person's own layer of the UI's shape, across devices
(``services/cowork_agent/storm/chats/shape.py``):

  GET /api/chat/shape    {shape: object | null, version, updated_at}
  PUT /api/chat/shape    {shape: object | null, if_version?}: null clears it

``if_version`` is the ``version`` the client read (``"new"`` for none): a
shape changed since on another device answers ``409 shape_changed`` with
``current``. ``400 bad_shape`` (not an object, nested too deep), ``413
shape_too_large`` (over 64 KB); 503 when the engine runs elsewhere.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter
from pydantic import Field

from routers.cowork_agent.bff.errors import ForbidExtra
from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import engine
from services.cowork_agent.storm.chats import shape
from services.errors import ServiceError

router = APIRouter()


class ShapeUpdate(ForbidExtra):
    shape: Optional[dict[str, Any]]
    if_version: Optional[str] = Field(None, min_length=1, max_length=32)


@router.get("/api/chat/shape")
async def get_shape() -> dict:
    try:
        return await shape.get(engine.current())
    except ServiceError as exc:
        raise storm_error(exc)


@router.put("/api/chat/shape")
async def put_shape(body: ShapeUpdate) -> dict:
    try:
        return await shape.put(engine.current(), body.shape, if_version=body.if_version)
    except ServiceError as exc:
        raise storm_error(exc)
