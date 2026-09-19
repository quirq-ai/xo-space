"""Per-request identity for Composio, in bring-your-own-key mode.

There is no XO sign-in: Composio runs on the user's own key (:mod:`.byo_key`). The
request is gated by the browser guard (a cross-site browser request is refused), and the
Composio ``user_id`` is ``XO_SPACE_ID`` (else a fixed default), the same value the key's
project is addressed by. No session bearer is involved.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from routers.browser_guard import origin_allowed
from modules.connectors.composio import byo_key

log = logging.getLogger(__name__)


async def resolve_user(request: Request) -> str | None:
    """The Composio user id for this request, or None when no key is configured.

    The soft paths (chat, ``/api/tools``) read None as "run without Composio tools".
    """
    if not byo_key.configured():
        return None
    return byo_key.user_id()


async def get_composio_user(request: Request) -> str:
    """FastAPI dependency for the Composio routes.

    403s a cross-site browser request; otherwise returns this pod's Composio ``user_id``.
    It does not require a key, so ``/toolkits`` and the key routes can report the
    "no key" state themselves rather than 403ing.
    """
    if not origin_allowed(request):
        raise HTTPException(status_code=403, detail="Cross-site request refused.")
    return byo_key.user_id()
