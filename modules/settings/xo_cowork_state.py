"""Quirq machine-local UI/installation state: the onboarding document.

Stored at ``~/.quirq/settings/onboarding.json`` (separate from the agent's
own home, such as ``~/.openclaw/``). This is for state that:

- belongs to Quirq (the product), not to the agent
- needs to persist across browsers/incognito/devtools-clear, so cannot
  live in localStorage
- is per-machine, not per-tenant (single-tenant assumption; revisit if
  xo-space ever serves multiple users from one process)

The document is flat; callers patch it via :func:`update_state`. First fields::

    {
      "onboarding_completed": bool,
      "onboarding_completed_at": "<iso-8601>"
    }

It is a :class:`services.storage.document.Document` (``store.onboarding_document``):
an absent file reads as empty, a file that is not JSON reads as empty and is
never rewritten (a write on top of it is refused with a 409 that names the
document), unknown keys survive a patch. The ``schema`` stamp is written,
``updated_at`` is not: the document has always carried only its own keys.

``STATE_DIR``, ``STATE_FILE`` and ``LEGACY_STATE_FILE`` are resolved once at
import, against the real state root; tests point them elsewhere and every
read and write goes through them at call time.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from services.storage.layout import settings_dir
from services.storage.paths import legacy_state_dir

from . import store

STATE_DIR = settings_dir()
STATE_FILE = STATE_DIR / "onboarding.json"
LEGACY_STATE_FILE = legacy_state_dir() / "state.json"

#: On-disk revision of the onboarding document.
STATE_SCHEMA = store.ONBOARDING_SCHEMA


def _document():
    return store.onboarding_document(STATE_FILE)


def _adopt_legacy() -> Optional[dict[str, Any]]:
    """Copy the pre-rename state (``~/.xo-cowork/state.json``) into place
    once, when nothing has been written under the state root yet. The old
    file is deliberately left untouched and is never a write target."""
    if STATE_FILE.exists() or not LEGACY_STATE_FILE.exists():
        return None
    try:
        payload = json.loads(LEGACY_STATE_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        _document().write(dict(payload), stamp=False)
    except OSError:
        pass
    return payload


def get_state() -> dict[str, Any]:
    adopted = _adopt_legacy()
    if adopted is not None:
        return adopted
    state, _ok = _document().read()
    return state


def update_state(patch: dict[str, Any]) -> dict[str, Any]:
    _adopt_legacy()

    def apply(document: dict) -> bool:
        document.update(patch)
        return True

    return _document().modify(apply, stamp=False)
