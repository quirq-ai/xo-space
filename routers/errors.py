"""The one HTTP seam for service failures, and the strict request-body base.

:func:`install_service_errors` registers the handler that turns any
:class:`services.errors.ServiceError` raised on a request path (a route, a
dependency such as a module gate, a service called from either) into the
answer every Space route has always given:
``HTTPException(status, {"code", "message"})``, on the wire
``{"detail": {"code": ..., "message": ...}}`` (or ``{"detail": message}``
for an error raised without a code, which only the schedules API does, as it
always has). Routers therefore carry no ``try/except`` and no mapping
helpers: the raiser picks the status.

A test that builds its own ``FastAPI()`` calls :func:`install_service_errors`
on it (``tests/support.client`` does).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from services.errors import ServiceError

logger = logging.getLogger("xo_space.errors")


async def service_error_response(_request: Request, exc: ServiceError) -> JSONResponse:
    if exc.log:
        logger.warning("%s (%s): %s", exc.code or exc.__class__.__name__, exc.status, exc.log)
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


def install_service_errors(app: FastAPI) -> FastAPI:
    """Attach the handler; returns ``app`` so a test can chain it."""
    app.add_exception_handler(ServiceError, service_error_response)
    return app


class ForbidExtra(BaseModel):
    """Request bodies are strict: an unknown key is a 422."""

    model_config = ConfigDict(extra="forbid")

    def given(self) -> dict[str, Any]:
        """The fields the caller actually sent (``None`` means "not sent"),
        so a service can merge them over the current record."""
        return {name: value for name, value in self.model_dump(exclude_unset=True).items() if value is not None}
