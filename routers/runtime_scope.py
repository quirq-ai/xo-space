"""The per-runtime prefix ``/api/chat/runtimes/{name}`` and the checks of its ``{name}``."""

from fastapi import HTTPException

from services.cowork_agent.registry.agent_registry import AgentManifest, UnknownRuntime, installed, runtime

PREFIX = "/api/chat/runtimes/{name}"


def _manifest(name: str) -> AgentManifest:
    try:
        return runtime(name)
    except UnknownRuntime as exc:
        raise HTTPException(status_code=404, detail={"code": "unknown_runtime", "message": str(exc)}) from None


def known_runtime(name: str) -> str:
    """``name`` when it is a runtime, else 404 ``unknown_runtime``."""
    return _manifest(name).name


def installed_runtime(name: str) -> str:
    """``name`` when it is an installed runtime, else 404 ``unknown_runtime`` or 409 ``not_installed``."""
    if not installed(_manifest(name).binary):
        raise HTTPException(status_code=409, detail={
            "code": "not_installed",
            "message": f"{name} isn't installed; POST /api/chat/runtimes/{name}/install installs it."})
    return name


def unsupported(name: str, what: str) -> HTTPException:
    """501 for a runtime without ``what``, the loader's rule for a missing capability."""
    return HTTPException(status_code=501, detail={"code": "unsupported", "message": f"{name} has no {what}."})
