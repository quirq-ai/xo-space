"""The connectors module's facade: connector status, in one place.

Each connector keeps its own package (``composio/``, ``gdrive/``,
``onedrive/``, ``github/``, ``vercel/``; ``rclone/`` is the engine behind the
two file stores and ``token_store`` the one owner of ``secrets/token.json``)
and their OAuth, device and token flows are called from the routes as
before. What the routes, ``commands.py`` and other modules ask this module
for is status: the per-connector functions are what the status routes
answer, and :func:`status` is every connector at once
(``python -m quirq connectors status``).

Every probe is read-only. A pasted token is checked live where its package
already does that (GitHub, Vercel); the rclone-backed stores answer whether
the daemon is reachable and which remotes it knows; MagicPath asks its CLI.
MagicPath has no package of its own yet (its CLI wrapper sits beside its
routes in ``routers/magicpath.py``), so that one probe is reached there.

Core code: names no agent and imports nothing from the adapters tree.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

from . import gdrive, github, onedrive, vercel
from .composio import byo_key
from .rclone import rclone_available

logger = logging.getLogger(__name__)

#: The connectors :func:`status` reports, in the order the Connectors tab shows them.
CONNECTORS = ("composio", "github", "vercel", "gdrive", "onedrive", "magicpath")


def composio_status() -> dict[str, Any]:
    """What ``GET /api/connectors/composio/backend`` answers: ``mode`` is
    ``"local"`` when a Composio API key is configured and ``"inactive"``
    otherwise; ``key_source`` says where the key came from (the environment,
    the key file, or ``None``)."""
    return {"mode": "local" if byo_key.configured() else "inactive", "key_source": byo_key.source()}


async def github_status() -> dict[str, Any]:
    """What ``GET /api/connectors/github/status`` answers: ``status`` and,
    when connected, the account and how it was connected (``github.get_status``)."""
    return await github.get_status()


async def vercel_status() -> vercel.Connection:
    """The stored Vercel connection, a pasted token checked live
    (``vercel.get_status``); ``as_dict()`` is the wire shape."""
    return await vercel.get_status()


async def gdrive_status() -> dict[str, Any]:
    """``{"available", "remotes"}``: whether the rclone daemon answers and,
    when it does, the Google Drive remotes it knows."""
    return await _rclone_store_status(gdrive.list_drive_remotes)


async def onedrive_status() -> dict[str, Any]:
    """``{"available", "remotes"}`` for OneDrive, the same way."""
    return await _rclone_store_status(onedrive.list_onedrive_remotes)


async def magicpath_status() -> dict[str, Any]:
    """What ``GET /api/connectors/magicpath/status`` answers: skill and CLI
    install state and the live login identity (a failed probe reads as not
    installed or not logged in, never as an error)."""
    from .routers.magicpath import probe_status

    return await probe_status()


async def _rclone_store_status(list_remotes: Callable[[], Awaitable[list]]) -> dict[str, Any]:
    if not await rclone_available():
        return {"available": False, "remotes": []}
    return {"available": True, "remotes": await list_remotes()}


async def status() -> dict[str, Any]:
    """Every connector's status: ``{"connectors": {name: status}}`` over
    :data:`CONNECTORS`. One probe failing (rclone missing, the network down)
    reports ``{"status": "error", "error": ...}`` for that connector and the
    others still answer. Nothing is written."""
    probes: dict[str, Callable[[], Any]] = {
        "composio": composio_status,
        "github": github_status,
        "vercel": vercel_status,
        "gdrive": gdrive_status,
        "onedrive": onedrive_status,
        "magicpath": magicpath_status,
    }
    out: dict[str, Any] = {}
    for name in CONNECTORS:
        try:
            result = probes[name]()
            if inspect.isawaitable(result):
                result = await result
            out[name] = result.as_dict() if hasattr(result, "as_dict") else result
        except Exception as exc:  # noqa: BLE001 - one connector must not hide the others
            logger.warning("connectors: %s status failed: %s", name, exc)
            out[name] = {"status": "error", "error": str(exc)}
    return {"connectors": out}
