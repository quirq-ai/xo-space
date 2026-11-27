"""Storm's runtimes (ch 17 §2.7):

  GET /api/chat/runtimes               each runtime's feature list, version, login and lifecycle; the server's own
                                       agent first, marked ``"default": true``; its ``health`` (usable now,
                                       and why not: login, failure, usage limit with its windows, each
                                       window's share used from the space advisory; the limit is null on
                                       a server that runs no engine). Asking reads usage again soon.
  GET /api/chat/runtimes/{name}/models that runtime's models and efforts
  GET /api/chat/runtime-policy         the Space's runtime policy and the one that applies
  PUT /api/chat/runtime-policy         {allowed?, default?, preferences?, fallback?}: replaces it
  GET /api/chat/chats/{chat}/runtime-policy   a project's override, the Space's, and the one that applies
  PUT /api/chat/chats/{chat}/runtime-policy   replaces the project's override (null fields follow the Space)
  POST /api/chat/chats/{chat}/threads/{ts}/continue   {agent?, model?, effort?, reason?}: the thread's work goes
                                       on in a new linked thread (the old one is stopped and frozen)

``{name}`` is validated against the discovered adapters (agent names are
data, never a branch). These read the runtimes, not the engine, so they
answer on any server.
"""

from __future__ import annotations

import asyncio
import dataclasses

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import Field

from routers.cowork_agent.bff.errors import ForbidExtra
from routers.cowork_agent.storm.errors import storm_error
from routers.runtime_scope import known_runtime, unsupported

from services import advisory as space_advisory
from services.cowork_agent.adapters.contract import Runtime, RuntimeFailure, RuntimeInfo
from services.cowork_agent.adapters.describe_once import describe_once
from services.cowork_agent.storm import engine as storm_engine, health, runtime_policy
from services.errors import ServiceError
from services.cowork_agent.storm.engine import EngineUnavailable, load_runtime, runtime_providers
from services.cowork_agent.registry.agent_registry import default_runtime
from services.cowork_agent.adapters.loader import list_capability_providers
from services.runtime_lifecycle import registry as runtime_lifecycle
from services.runtime_lifecycle.state import state as lifecycle_state

router = APIRouter()


def _pools():
    """The running engine's pools, or None when this server runs none."""
    try:
        return storm_engine.current().pool
    except EngineUnavailable:
        return None


async def _describe(name: str, runtime: Runtime, default: str) -> dict:
    space_advisory.current().request_read(name)       # someone is looking: its usage is read again soon
    entry: dict = {"name": name, "default": name == default}
    try:
        # Once, bounded: health and the CLI's entry read this answer. A runtime keeps its CLI checks until
        # what they read changes, so this waits on a CLI only the first time, or after an update or sign-in.
        info = await asyncio.wait_for(describe_once(name, runtime), health.CHECK_WAIT_S)
        entry["cli"] = {"version": info.cli_version, "logged_in": info.logged_in, "account": info.account,
                        "limits": dict(info.limits)}
    except RuntimeFailure as exc:
        info, entry["cli"] = exc, None
        entry["error"] = {"code": exc.code, "message": exc.message}
    except TimeoutError:
        # Its check goes on (``describe_once``) and is kept, so a later read is quick; meanwhile it doesn't
        # hold the others' answer.
        info, entry["cli"], entry["error"] = None, None, health.slow_error()
    entry["health"] = (health.described(name, info, _pools()) if info is not None else health.slow(name)).as_dict()
    entry["features"] = dataclasses.asdict(runtime.features())    # after describe: it knows the CLI's version
    if name in list_capability_providers("lifecycle"):
        # Only after describe() answered is a login that is a CLI run read (kept by then): while it still
        # checks, or when it failed, the login run would be unbounded inside this bounded list. Its jobs and
        # actions are still said, with ``signed_in`` null, not known yet.
        entry["lifecycle"] = await lifecycle_state(runtime_lifecycle.get(name),
                                                   read_login=isinstance(info, RuntimeInfo),
                                                   version=(entry["cli"] or {}).get("version"))
    return entry


@router.get("/api/chat/runtimes")
async def list_runtimes() -> dict:
    """The server's agent (what a new chat runs on) comes first, so a client
    that takes the first entry picks it."""
    default = default_runtime()
    found = [(name, rt) for name in runtime_providers() if (rt := load_runtime(name)) is not None]
    found.sort(key=lambda pair: pair[0] != default)
    return {"runtimes": list(await asyncio.gather(*(_describe(name, rt, default) for name, rt in found)))}


@router.get("/api/chat/runtimes/{name}/models")
async def runtime_models(name: str) -> dict:
    runtime = load_runtime(known_runtime(name))
    if runtime is None:
        raise unsupported(name, "runtime")
    try:
        models = await runtime.models()
    except RuntimeFailure as exc:
        raise HTTPException(status_code=503, detail={"code": "runtime_unavailable", "message": exc.message})
    return {"name": name, "models": [dataclasses.asdict(m) for m in models]}


class RuntimePolicy(ForbidExtra):
    allowed: Optional[list[str]] = Field(None, max_length=32)
    default: Optional[str] = Field(None, max_length=64)
    preferences: Optional[str] = Field(None, max_length=runtime_policy.PREFERENCES_MAX)
    fallback: Optional[bool] = None


@router.get("/api/chat/runtime-policy")
async def get_space_policy() -> dict:
    try:
        return await runtime_policy.read(storm_engine.current())
    except ServiceError as exc:
        raise storm_error(exc)


@router.put("/api/chat/runtime-policy")
async def put_space_policy(body: RuntimePolicy) -> dict:
    try:
        return await runtime_policy.write(storm_engine.current(), body.model_dump(), runtime_providers())
    except ServiceError as exc:
        raise storm_error(exc)


@router.get("/api/chat/chats/{chat_id}/runtime-policy")
async def get_project_policy(chat_id: str) -> dict:
    try:
        return await runtime_policy.read(storm_engine.current(), chat_id)
    except ServiceError as exc:
        raise storm_error(exc)


@router.put("/api/chat/chats/{chat_id}/runtime-policy")
async def put_project_policy(chat_id: str, body: RuntimePolicy) -> dict:
    try:
        return await runtime_policy.write(storm_engine.current(), body.model_dump(), runtime_providers(), chat_id)
    except ServiceError as exc:
        raise storm_error(exc)


class ContinueThread(ForbidExtra):
    agent: Optional[str] = Field(None, max_length=64)
    model: Optional[str] = Field(None, max_length=200)
    effort: Optional[str] = Field(None, max_length=16)
    reason: str = Field("the owner moved it", min_length=1, max_length=200)


@router.post("/api/chat/chats/{chat_id}/threads/{ts}/continue")
async def continue_thread(chat_id: str, ts: str, body: ContinueThread) -> dict:
    from services.cowork_agent.storm import chats as storm_chats
    from services.cowork_agent.storm.chats import continuation
    try:
        service = storm_chats.current()
        return await continuation.continue_thread(service, await service.chat(chat_id), ts, agent=body.agent,
                                                  model=body.model, effort=body.effort, reason=body.reason,
                                                  by="owner")
    except ServiceError as exc:
        raise storm_error(exc)
