"""Where Composio's two local stores live: ``sessions.json`` and ``action_prefs.json``.

Both sit in the user's config directory, never the checkout — the same rule
:mod:`services.cowork_agent.connectors.token_store` follows for ``token.json``, and for
the same reason. ``sessions.json`` holds live MCP proxy tokens in plaintext; a store that
dies with a fresh clone, a redeploy or an ``uninstall`` leaves every agent holding a token
this pod can no longer resolve, and they all 401 until their config is rewritten.

``COMPOSIO_STORE_DIR`` moves the pair in one variable — a mounted volume, ``~/.composio``,
wherever. Deliberately *not* named ``COMPOSIO_STATE_*``: that family belongs to the
xo-swarm-api HTTP client in :mod:`.state`, where ``COMPOSIO_STATE_PATH`` is a URL path, not
a filesystem one.

The default is ``~/.config/composio/`` rather than ``~/.composio/`` because the Composio
SDK owns the latter — it caches downloads in ``~/.composio/files`` and treats
``~/.composio/temp`` as its default allowlisted upload directory. A 0600 credential does
not belong in a directory a third-party library manages as scratch space.

This module owns the directory and the one-shot move out of the checkout, and nothing
else. Each store's filename, on-disk shape and read/write semantics stay with its own
owner — :mod:`.service` for the sessions document, :mod:`.action_prefs` for the prefs one.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_STORE_DIR = Path.home() / ".config" / "composio"

# connectors/composio/ → connectors/ → cowork_agent/ → services/ → repo root. The only
# remaining reason this package knows where the checkout is: finding what to migrate.
_CHECKOUT_DATA_DIR = Path(__file__).resolve().parents[4] / "data"


def store_dir() -> Path:
    """The directory both stores live in. Never created here — the writer mkdirs."""
    configured = (os.getenv("COMPOSIO_STORE_DIR", "") or "").strip()
    return Path(configured).expanduser() if configured else _DEFAULT_STORE_DIR


def legacy_checkout_path(name: str) -> Path:
    """Where ``name`` used to sit, back when these stores lived in the checkout."""
    return _CHECKOUT_DATA_DIR / name


def migrate_legacy(
    target: Path, legacy_paths: tuple[Path, ...], *, mode: int | None = None
) -> None:
    """Move a store left at an older location to ``target``, once.

    No-op when the store is already in place or nothing was left behind. A legacy path
    that ``target`` itself points at is skipped, so an override aimed at the old location
    keeps working. ``mode`` is applied after the move for a store that is a credential;
    prefs pass None and keep the default.

    A failure here is logged, never raised: the caller then sees an empty store — the
    same degradation as a store that was never written — rather than a crashed connector.
    """
    if target.exists():
        return
    for legacy in legacy_paths:
        if legacy == target or not legacy.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(target))
            if mode is not None:
                os.chmod(target, mode)
            log.info("Moved Composio store %s -> %s", legacy, target)
        except OSError as exc:  # e.g. a mount that ignores chmod, or a read-only checkout
            log.warning("Could not move %s to %s: %s", legacy, target, exc)
        return
