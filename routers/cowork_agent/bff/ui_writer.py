"""The UI writer key on the routes only the person may call through the
coworker UI (``services/project_files/writer_key``): its proxy adds
``X-XO-UI-Writer`` after its own same-origin check, so a script an agent
runs, which reaches Space with no ``Origin``, can't pass for the person.

The FastAPI dependency a destructive route declares (deleting a thread and
its undo), so a route function stays a plain function its tests can call
directly.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException

from services.project_files import writer_key


def require_ui_writer(x_xo_ui_writer: Optional[str] = Header(default=None)) -> None:
    if not writer_key.matches(x_xo_ui_writer):
        raise HTTPException(status_code=403, detail={
            "code": "ui_only",
            "message": "Only the person can do this, through the XO UI (its proxy adds X-XO-UI-Writer). Agents "
                       "ask the person instead.",
        })
