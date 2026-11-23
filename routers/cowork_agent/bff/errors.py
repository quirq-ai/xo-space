"""HTTP glue shared by the Space BFF routes (``inbox.py``, ``connections.py``,
the project files routes and the coworker add-ons).

:func:`http_error` turns a :class:`services.errors.ServiceError` into the
``HTTPException(status, {"code", "message"})`` every Space route answers
with; :func:`refused_response` is the same answer as a response, plus what a
file refusal carries; :class:`ForbidExtra` is the request-body base that
makes an unknown key a 422.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from services.errors import ServiceError
from services.project_files.address import FileRefused


def http_error(exc: ServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


def refused_response(exc: ServiceError) -> JSONResponse:
    """The ``{"detail": {code, message, …}}`` every Space route answers with,
    plus what a refusal carries (a conflict's ``current``)."""
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if isinstance(exc, FileRefused):
        detail.update(exc.extra)
    return JSONResponse(status_code=exc.status, content={"detail": detail})


class ForbidExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")
