"""
Quirq machine-local UI/installation state.

Stored at ~/.quirq/state.json (separate from ~/.openclaw/, which is
OpenClaw's own data). This is for state that:

- belongs to Quirq (the product), not to OpenClaw
- needs to persist across browsers/incognito/devtools-clear, so cannot
  live in localStorage
- is per-machine, not per-tenant (single-tenant assumption — revisit if
  xo-space ever serves multiple users from one process)

Schema is a flat dict; callers patch it via `update_state`. First fields:

    {
      "onboarding_completed": bool,
      "onboarding_completed_at": "<iso-8601>"
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from services.cowork_agent.local_state import legacy_state_dir, quirq_state_dir


STATE_DIR = quirq_state_dir()
STATE_FILE = STATE_DIR / "state.json"
LEGACY_STATE_FILE = legacy_state_dir() / "state.json"


def _read() -> dict[str, Any]:
    source_path = STATE_FILE if STATE_FILE.exists() else LEGACY_STATE_FILE
    if not source_path.exists():
        return {}
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}
    if source_path == LEGACY_STATE_FILE and isinstance(payload, dict):
        # Preserve onboarding across the directory rename. The old file is
        # deliberately left untouched and is never a write target.
        try:
            _atomic_write(payload)
        except OSError:
            pass
    return payload if isinstance(payload, dict) else {}


def _atomic_write(payload: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_state() -> dict[str, Any]:
    return _read()


def update_state(patch: dict[str, Any]) -> dict[str, Any]:
    current = _read()
    current.update(patch)
    _atomic_write(current)
    return current
