"""Canonical machine-local state roots for Quirq.

``~/.quirq/`` belongs to the local Quirq installation. It is separate
from portable project metadata under ``<project>/.xo/`` and from each
agent runtime's native storage.

``~/.xo-cowork/`` is retained only as a read-only migration source.
Callers must write new state beneath :func:`quirq_state_dir`.
"""

from __future__ import annotations

from pathlib import Path

# Defined once, below the services layer, so utils/ code (the command
# scheduler) can share it without importing services/. This module stays the
# documented entry point for service code.
from utils.runtime_env import quirq_state_dir  # noqa: F401  (re-export)


def legacy_state_dir() -> Path:
    """Return the former state root, used only for one-time migration."""
    return Path.home() / ".xo-cowork"
