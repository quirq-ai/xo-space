"""Remote control for the Space's agent (``/api/remote-control/*``) and for each runtime by name, resolved per call."""

from __future__ import annotations

import inspect
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from routers.runtime_scope import PREFIX, known_runtime, unsupported
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.registry.agent_registry import space_agent


class StartBody(BaseModel):
    name: str | None = None


class PairingStatusBody(BaseModel):
    """One of the codes a ``pair`` call returned (sent in the body, never the URL)."""
    pairing_code: str | None = None
    manual_pairing_code: str | None = None


def _endpoint(attr: str) -> Callable[[Request], Callable[..., Any]]:
    """A dependency giving the answering runtime's ``remote_control.<attr>``."""
    def resolve(request: Request) -> Callable[..., Any]:
        name = request.path_params.get("name")
        fn = getattr(try_load_capability("remote_control", agent=name or space_agent()), attr, None)
        if fn is None:
            raise unsupported(name, "remote control") if name else HTTPException(status_code=404, detail="Not Found")
        return fn
    return resolve


async def _call(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Await an async endpoint; run a blocking one off the event loop."""
    if inspect.iscoroutinefunction(fn):
        return await fn(**kwargs)
    return await run_in_threadpool(fn, **kwargs)


_routes = APIRouter()


@_routes.get("/status")
async def remote_control_status(fn=Depends(_endpoint("status"))) -> dict[str, Any]:
    return await _call(fn)


@_routes.post("/start")
async def remote_control_start(body: StartBody | None = None, fn=Depends(_endpoint("start"))) -> dict[str, Any]:
    return await _call(fn, name=body.name if body else None)


@_routes.post("/stop")
async def remote_control_stop(fn=Depends(_endpoint("stop"))) -> dict[str, Any]:
    return await _call(fn)


@_routes.post("/pair")
async def remote_control_pair(fn=Depends(_endpoint("pair"))) -> dict[str, Any]:
    return await _call(fn)


@_routes.post("/pair/status")
async def remote_control_pair_status(body: PairingStatusBody,
                                     fn=Depends(_endpoint("pairing_status"))) -> dict[str, Any]:
    return await _call(fn, pairing_code=body.pairing_code, manual_pairing_code=body.manual_pairing_code)


router = APIRouter(tags=["remote-control"])
router.include_router(_routes, prefix="/api/remote-control")
router.include_router(_routes, prefix=f"{PREFIX}/remote-control", dependencies=[Depends(known_runtime)])
