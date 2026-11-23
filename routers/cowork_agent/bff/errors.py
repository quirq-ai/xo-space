"""HTTP glue for Space BFF routes: ServiceError to HTTPException/response, and ForbidExtra (unknown key = 422)."""

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
    """The ``{"detail": {code, message, ...}}`` response for a ServiceError, plus a FileRefused's extras."""
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if isinstance(exc, FileRefused):
        detail.update(exc.extra)
    return JSONResponse(status_code=exc.status, content={"detail": detail})


class ForbidExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")
