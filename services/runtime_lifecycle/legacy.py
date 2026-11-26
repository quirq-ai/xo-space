"""The old Connect URLs each runtime's manifest declares (``legacy_paths``),
served by the engine with their old answers (the runtime's ``Legacy`` view
and callback message), so core spells none of them: ``sign_in`` paths take
the stream (``?force=true`` drops a running sign-in first), ``/callback``
(paste-code runtimes) and DELETE; ``update`` paths stream the update job in the old Connect install
events (``installing``, ``install_log``, ``done`` or ``error``)."""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from services.cowork_agent.storm.records.ledger import FOUNDER
from services.runtime_lifecycle import jobs, signin
from services.runtime_lifecycle.contract import LifecycleError
from services.runtime_lifecycle.registry import Runtime, runtimes

_OLD_NAMES = {"started": "installing", "log": "install_log"}


async def _update_frames(rt: Runtime) -> AsyncIterator[str]:
    try:
        job = await jobs.start(rt, "update", by=FOUNDER)
    except LifecycleError as exc:
        yield signin.error_frame(exc.message)
        return
    async for event in job.follow(signin.HEARTBEAT_S):
        if event["type"] == "heartbeat":
            yield ": heartbeat\n\n"
        elif event["type"] == "error":
            yield signin.error_frame(event["message"])
        else:
            yield signin.frame({**event, "type": _OLD_NAMES.get(event["type"], event["type"])})


def _sign_in(router: APIRouter, rt: Runtime, path: str) -> None:
    async def start(force: bool = False) -> StreamingResponse:
        if force:
            signin.cancel(rt)
        return signin.sse(signin.stream(rt, view=rt.spec.legacy.view))

    async def callback(body: signin.CodeBody) -> dict:
        try:
            return await signin.submit(rt, body.session_id, body.code, start_path=path,
                                       sent=rt.spec.legacy.code_sent)
        except LifecycleError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from None

    async def cancel() -> dict:
        return signin.cancel(rt)

    router.add_api_route(path, start, methods=["POST"])
    router.add_api_route(path, cancel, methods=["DELETE"])
    if rt.spec.sign_in is not None and rt.spec.sign_in.style == "paste_code":
        router.add_api_route(path + "/callback", callback, methods=["POST"])


def _update(router: APIRouter, rt: Runtime, path: str) -> None:
    async def update() -> StreamingResponse:
        return signin.sse(_update_frames(rt))

    router.add_api_route(path, update, methods=["POST"])


def router() -> APIRouter:
    found = APIRouter(tags=["runtime-lifecycle"])
    for rt in runtimes().values():
        for path in rt.legacy_paths("sign_in"):
            _sign_in(found, rt, path)
        for path in rt.legacy_paths("update"):
            _update(found, rt, path)
    return found
