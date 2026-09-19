"""HTTP glue shared by every router: the ``ServiceError`` mapping and the strict body base.

:func:`http_error` turns a :class:`services.errors.ServiceError` into the
``HTTPException(status, {"code", "message"})`` every Space route answers
with; :class:`ForbidExtra` is the request-body base that makes an unknown
key a 422.
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from services.errors import ServiceError


def http_error(exc: ServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})


class ForbidExtra(BaseModel):
    model_config = ConfigDict(extra="forbid")
