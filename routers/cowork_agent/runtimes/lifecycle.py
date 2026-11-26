"""A runtime's lifecycle (plan §5.3, §5.4); the engine is ``services/runtime_lifecycle``.

  GET    /api/chat/runtimes/{name}/lifecycle        its state, version and the actions it offers
  POST   /api/chat/runtimes/{name}/sign-in          SSE: session, auth_url, stdout, done | error
  POST   /api/chat/runtimes/{name}/sign-in/callback {code, session_id}: the pasted code
  DELETE /api/chat/runtimes/{name}/sign-in          cancels the running sign-in
  POST   /api/chat/runtimes/{name}/{action}         install | update | uninstall | sign-out: 202 and the job
  GET    /api/chat/runtimes/{name}/jobs/{id}        the job: status, log, result

Uninstalling a runtime that sessions run on is refused (409 ``runtime_in_use``
with them) unless ``force``, which stops them first.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from routers.cowork_agent.bff.errors import ForbidExtra
from services.cowork_agent.storm.records.ledger import FOUNDER
from services.runtime_lifecycle import jobs, registry, signin
from services.runtime_lifecycle.contract import LifecycleError
from services.runtime_lifecycle.state import state

router = APIRouter(tags=["runtime-lifecycle"])


class ActionBody(ForbidExtra):
    force: bool = False


def _refused(exc: LifecycleError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message, **exc.extra})


def _runtime(name: str) -> registry.Runtime:
    try:
        return registry.get(name)
    except LifecycleError as exc:
        raise _refused(exc) from None


@router.get("/api/chat/runtimes/{name}/lifecycle")
async def runtime_lifecycle(name: str) -> dict:
    rt = _runtime(name)
    binary = rt.spec.binary()
    return await state(rt, version=await jobs.version_of(binary) if registry.installed(binary) else None)


@router.post("/api/chat/runtimes/{name}/sign-in")
async def sign_in(name: str) -> StreamingResponse:
    return signin.sse(signin.stream(_runtime(name), install_path=f"/api/chat/runtimes/{name}/install"))


@router.post("/api/chat/runtimes/{name}/sign-in/callback")
async def sign_in_callback(name: str, body: signin.CodeBody) -> dict:
    try:
        return await signin.submit(_runtime(name), body.session_id, body.code,
                                   start_path=f"/api/chat/runtimes/{name}/sign-in")
    except LifecycleError as exc:
        raise _refused(exc) from None


@router.delete("/api/chat/runtimes/{name}/sign-in")
async def cancel_sign_in(name: str) -> dict:
    return signin.cancel(_runtime(name))


def _job_route(path: str, action: str) -> None:
    async def start_job(name: str, body: ActionBody | None = None) -> JSONResponse:
        try:
            job = await jobs.start(_runtime(name), action, force=bool(body and body.force), by=FOUNDER)
        except LifecycleError as exc:
            raise _refused(exc) from None
        return JSONResponse(job.as_dict(), status_code=202)

    router.add_api_route(f"/api/chat/runtimes/{{name}}/{path}", start_job, methods=["POST"], status_code=202)


for _path, _action in {"install": "install", "update": "update", "uninstall": "uninstall",
                       "sign-out": "sign_out"}.items():
    _job_route(_path, _action)


@router.get("/api/chat/runtimes/{name}/jobs/{job_id}")
async def get_job(name: str, job_id: str) -> dict:
    try:
        return jobs.get(name, job_id).as_dict()
    except LifecycleError as exc:
        raise _refused(exc) from None
