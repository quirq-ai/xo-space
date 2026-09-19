"""Centralised scope-to-handle resolution for the BFF layer.

:class:`SecretsScope` belongs to settings: it lives in
``modules.settings.service`` and is re-exported from here. The project
handles, :class:`VisualizerScope` and :class:`WorkspaceVisualizerScope`,
moved to ``modules.projects.service`` and are re-exported from here, the
same objects, so every importer and every patch through this module keeps
working. :func:`resolve_scope` answers ``"secrets"`` itself and hands every
other name to the projects module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from modules.projects.service import (  # noqa: F401  (re-exported)
    ScopeNotFound,
    VisualizerScope,
    WorkitemRollup,
    WorkspaceVisualizerScope,
)
from modules.projects import service as _projects
from modules.settings.service import SecretsScope  # noqa: F401  (re-exported: it belongs to settings)


# ── Resolver ──────────────────────────────────────────────────────────────────


ScopeHandle = Union[Path, SecretsScope, VisualizerScope, WorkspaceVisualizerScope]


def resolve_scope(name: str, *args) -> ScopeHandle:
    """Resolve a scope name to its handle.

    Returns a ``Path`` for filesystem scopes, or a domain-specific
    handle (``SecretsScope``, ``VisualizerScope``,
    ``WorkspaceVisualizerScope``).

    Variadic ``args`` carry scope-specific positional inputs:
    ``"xo-projects-visualizer"`` takes a ``project_id``; the others
    take none. ``"secrets"`` is answered here; the project scopes by
    ``modules.projects.service.resolve_scope``.
    """
    if name == "secrets":
        return SecretsScope()
    return _projects.resolve_scope(name, *args)
