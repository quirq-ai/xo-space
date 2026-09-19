"""The typed failure every Space service raises.

A service never imports FastAPI: it raises a :class:`ServiceError` and the
one handler ``routers/errors.py`` installs on the app turns it into
``{"detail": {"code", "message"}}`` with the status the raiser chose. There
is no per-router mapping; the place that knows why something failed also
knows what status it deserves.

``code`` may be ``None`` for the one surface whose ``detail`` has always
been the bare message (``/api/schedules``): the handler then answers
``{"detail": message}``. Nothing new should raise without a code.
"""

from __future__ import annotations

from typing import Any, Optional


class ServiceError(Exception):
    """``code`` names the failure for the client, ``message`` explains it
    (safe to show a person: it never names a filesystem path), ``status`` is
    the HTTP status the handler answers with (400 unless the raiser says
    otherwise) and ``log``, when given, is the detail for the server log
    only (the path, the parse error), never the wire."""

    def __init__(self, code: Optional[str], message: str, status: int = 400, *, log: Optional[str] = None) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.log = code, message, status, log

    @property
    def detail(self) -> Any:
        """The wire ``detail``: ``{"code", "message"}``, or the bare message
        when the raiser gave no code."""
        if self.code is None:
            return self.message
        return {"code": self.code, "message": self.message}


class NotFound(ServiceError):
    """A record, module or item the caller named does not exist (404)."""

    def __init__(self, code: str, message: str, *, log: Optional[str] = None) -> None:
        super().__init__(code, message, 404, log=log)


class Conflict(ServiceError):
    """The request is valid but the current state refuses it (409)."""

    def __init__(self, code: str, message: str, *, log: Optional[str] = None) -> None:
        super().__init__(code, message, 409, log=log)


class Unavailable(ServiceError):
    """A dependency the request needs is missing or broken (503)."""

    def __init__(self, code: str, message: str, *, log: Optional[str] = None) -> None:
        super().__init__(code, message, 503, log=log)
