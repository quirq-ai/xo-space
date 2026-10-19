"""The Space's router and its decision models (design ch 17 §2.6; ch 11
§14.1–§14.3; §0.81):

  GET    /api/chat/router                        {mode, decision_model, abstain_below}
  PUT    /api/chat/router                        the same; on by default (agent)
  GET    /api/chat/router/models                 each provider, its model and whether it is connected
  POST   /api/chat/router/models                 {provider, model?, api_key}
  DELETE /api/chat/router/models/{provider}

A key goes to ``~/.quirq/secrets/router.env`` and is never shown again: not
here, not in ``/api/secrets/env``, not in ``/providers/status``.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter
from pydantic import Field

from routers.cowork_agent.bff.errors import ForbidExtra
from routers.cowork_agent.storm.errors import storm_error
from services.cowork_agent.storm import engine
from services.cowork_agent.storm.chats import router as storm_router
from services.errors import ServiceError

router = APIRouter()


class RouterSettings(ForbidExtra):
    mode: Literal["off", "agent", "decision"]
    decision_model: Optional[str] = Field(None, max_length=64)
    abstain_below: float = Field(storm_router.DEFAULT_ABSTAIN_BELOW, ge=0.0, le=1.0)


class DecisionModel(ForbidExtra):
    provider: str = Field(min_length=1, max_length=64)
    model: Optional[str] = Field(None, max_length=200)
    api_key: str = Field(min_length=1, max_length=storm_router.API_KEY_MAX)


@router.get("/api/chat/router")
async def get_router() -> dict:
    try:
        return await storm_router.get(engine.current())
    except ServiceError as exc:
        raise storm_error(exc)


@router.put("/api/chat/router")
async def put_router(body: RouterSettings) -> dict:
    try:
        return await storm_router.put(engine.current(), mode=body.mode, decision_model=body.decision_model,
                                      abstain_below=body.abstain_below)
    except ServiceError as exc:
        raise storm_error(exc)


@router.get("/api/chat/router/models")
async def list_models() -> dict:
    try:
        return {"models": await storm_router.models(engine.current())}
    except ServiceError as exc:
        raise storm_error(exc)


@router.post("/api/chat/router/models")
async def connect_model(body: DecisionModel) -> dict:
    try:
        return {"models": await storm_router.connect(engine.current(), provider=body.provider, model=body.model,
                                                     api_key=body.api_key)}
    except ServiceError as exc:
        raise storm_error(exc)


@router.delete("/api/chat/router/models/{provider}")
async def disconnect_model(provider: str) -> dict:
    try:
        return {"models": await storm_router.disconnect(engine.current(), provider)}
    except ServiceError as exc:
        raise storm_error(exc)
