"""The typed failure the Space services raise.

A service package (``services/inbox``, ``services/connections``) never
imports FastAPI: it raises a :class:`ServiceError` subclass and the BFF
route turns it into ``HTTPException(status, {"code", "message"})`` through
``routers.errors.http_error``.
"""

from __future__ import annotations


class ServiceError(Exception):
    """``code`` names the failure for the client, ``message`` explains it and
    ``status`` is the HTTP status the router answers with (400 unless the
    raiser says otherwise)."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
