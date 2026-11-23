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


ONLY_THE_PERSON = ("Only the person can do this, through the XO UI (its proxy adds X-XO-UI-Writer). Agents "
                   "ask the person instead.")


def check_ui_writer(presented: Optional[str], message: str = ONLY_THE_PERSON) -> None:
    """403 ``ui_only`` with ``message`` unless ``presented`` is the UI
    writer key; for a route that reads the header itself."""
    if not writer_key.matches(presented):
        raise HTTPException(status_code=403, detail={"code": "ui_only", "message": message})


def require_ui_writer(x_xo_ui_writer: Optional[str] = Header(default=None)) -> None:
    check_ui_writer(x_xo_ui_writer)
