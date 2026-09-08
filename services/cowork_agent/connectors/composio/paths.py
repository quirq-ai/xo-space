"""Where Composio's local stores live: ``sessions.json`` and ``action_prefs.json``.

Overridable with ``COMPOSIO_STORE_DIR``. The default is ``~/.config/composio/``, not
``~/.composio/``, which the Composio SDK owns as scratch space.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_STORE_DIR = Path.home() / ".config" / "composio"

# connectors/composio/ → connectors/ → cowork_agent/ → services/ → repo root.
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

    No-op when the store is already in place or nothing was left behind. Failures are
    logged, never raised.
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
        except OSError as exc:
            log.warning("Could not move %s to %s: %s", legacy, target, exc)
        return
