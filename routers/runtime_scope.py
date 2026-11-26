"""The per-runtime prefix ``/api/chat/runtimes/{name}`` and the check of its ``{name}``."""

from fastapi import HTTPException

from services.cowork_agent.registry.agent_registry import runtimes

PREFIX = "/api/chat/runtimes/{name}"


def known_runtime(name: str) -> str:
    """``name`` when it is a runtime, else 404 ``unknown_runtime`` (as ``/models`` answers)."""
    if name not in {m.name for m in runtimes()}:
        raise HTTPException(status_code=404, detail={
            "code": "unknown_runtime", "message": f"No storm runtime is named {name!r}; GET /api/chat/runtimes "
                                                  "lists them."})
    return name


def unsupported(name: str, what: str) -> HTTPException:
    """501 for a runtime without ``what``, the loader's rule for a missing capability."""
    return HTTPException(status_code=501, detail={"code": "unsupported", "message": f"{name} has no {what}."})
