"""Compatibility alias: the HTTP error seam moved to ``routers/errors.py``.

``ForbidExtra`` is the same class. :func:`http_error` is kept for one
release for code outside this repository that imported it; routes in this
repository raise :class:`services.errors.ServiceError` and let the app
handler answer.
"""

from __future__ import annotations

from fastapi import HTTPException

from routers.errors import ForbidExtra  # noqa: F401  (re-export)
from services.errors import ServiceError


def http_error(exc: ServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})
