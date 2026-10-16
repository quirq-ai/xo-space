"""Storm's runtimes (ch 17 §2.7):

  GET /api/chat/runtimes               each runtime's feature list, version and login
  GET /api/chat/runtimes/{name}/models that runtime's models and efforts

``{name}`` is validated against the discovered adapters (agent names are
data, never a branch). These read the runtimes, not the engine, so they
answer on any server.
"""

from __future__ import annotations

import asyncio
import dataclasses

from fastapi import APIRouter, HTTPException

from services.cowork_agent.adapters.contract import Runtime, RuntimeFailure
from services.cowork_agent.storm.engine import load_runtime, runtime_providers

router = APIRouter()


async def _describe(name: str, runtime: Runtime) -> dict:
    entry: dict = {"name": name}
    try:
        info = await runtime.describe()
        entry["cli"] = {"version": info.cli_version, "logged_in": info.logged_in, "account": info.account,
                        "limits": dict(info.limits)}
    except RuntimeFailure as exc:
        entry["cli"] = None
        entry["error"] = {"code": exc.code, "message": exc.message}
    entry["features"] = dataclasses.asdict(runtime.features())    # after describe: it knows the CLI's version
    return entry


@router.get("/api/chat/runtimes")
async def list_runtimes() -> dict:
    found = [(name, rt) for name in runtime_providers() if (rt := load_runtime(name)) is not None]
    return {"runtimes": list(await asyncio.gather(*(_describe(name, rt) for name, rt in found)))}


@router.get("/api/chat/runtimes/{name}/models")
async def runtime_models(name: str) -> dict:
    runtime = load_runtime(name) if name in runtime_providers() else None
    if runtime is None:
        raise HTTPException(status_code=404, detail={
            "code": "unknown_runtime", "message": f"No storm runtime is named {name!r}; GET /api/chat/runtimes "
                                                  "lists them."})
    try:
        models = await runtime.models()
    except RuntimeFailure as exc:
        raise HTTPException(status_code=503, detail={"code": "runtime_unavailable", "message": exc.message})
    return {"name": name, "models": [dataclasses.asdict(m) for m in models]}
