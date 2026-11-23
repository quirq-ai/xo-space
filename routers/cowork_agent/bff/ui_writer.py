"""UI writer key check for person-only routes; agent scripts (no Origin) can't get the proxy to add the header."""

from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException

from services.project_files import writer_key


ONLY_THE_PERSON = ("Only the person can do this, through the XO UI (its proxy adds X-XO-UI-Writer). Agents "
                   "ask the person instead.")


def check_ui_writer(presented: Optional[str], message: str = ONLY_THE_PERSON) -> None:
    """Raise 403 ``ui_only`` with ``message`` unless ``presented`` is the UI writer key."""
    if not writer_key.matches(presented):
        raise HTTPException(status_code=403, detail={"code": "ui_only", "message": message})


def require_ui_writer(x_xo_ui_writer: Optional[str] = Header(default=None)) -> None:
    check_ui_writer(x_xo_ui_writer)
