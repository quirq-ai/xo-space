"""
Dynamic capability resolver — the single seam every agent-specific module is
reached through.

Everything agent-specific lives at
``services.cowork_agent.adapters.<AGENT_NAME>.<capability>`` and is resolved
from the active agent (``AGENT_NAME`` → ``DEFAULT_AGENT`` → single-manifest
auto-pick, via ``get_active_agent()``). No core module names a specific agent;
it asks for a *capability* and the loader imports the active agent's
implementation.

Capabilities are just module names inside the adapter package, e.g.
``adapter``, ``usage``, ``models_status``, ``channels_status``,
``providers_status``, ``visualizer_source``, ``routes``.

Adding a new agent is "drop services/cowork_agent/adapters/<name>/" — no edits
to any core file.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import services.cowork_agent.adapters as _adapters_pkg

from services.cowork_agent.registry.agent_registry import get_active_agent


_ADAPTERS_DIR = Path(_adapters_pkg.__file__).resolve().parent


def _module_name(capability: str, agent: str) -> str:
    if not capability.isidentifier():
        raise ValueError(f"invalid capability name: {capability!r}")
    if not agent.isidentifier():
        raise ValueError(f"invalid agent name: {agent!r}")
    return f"services.cowork_agent.adapters.{agent}.{capability}"


def load_capability(capability: str, *, agent: str | None = None) -> ModuleType:
    """Import the (active) agent's implementation of ``capability``.

    Args:
        capability: module name under the adapter package (e.g. ``"usage"``).
        agent: explicit agent name; defaults to the active agent.

    Raises:
        ModuleNotFoundError: if the agent has no
            ``services/cowork_agent/adapters/<name>/<capability>.py``. The
            error names the import path so the fix is obvious.
    """
    name = agent or get_active_agent().name
    return importlib.import_module(_module_name(capability, name))


def try_load_capability(capability: str, *, agent: str | None = None) -> ModuleType | None:
    """Like :func:`load_capability` but returns ``None`` when the active agent
    does not implement ``capability`` (instead of raising).

    Use for optional capabilities — e.g. agent-owned ``routes`` that only some
    agents provide.
    """
    name = agent or get_active_agent().name
    expected = _module_name(capability, name)
    provider_package = expected.rsplit(".", 1)[0]
    try:
        return importlib.import_module(expected)
    except ModuleNotFoundError as exc:
        # A missing optional capability is normal. A missing dependency inside
        # an otherwise-present capability is a real implementation error and
        # must not be disguised as "unsupported".
        if exc.name not in {expected, provider_package}:
            raise
        return None


def list_capability_providers(capability: str) -> list[str]:
    """Return every adapter package that implements ``capability``.

    Most broker capabilities resolve only for the active agent. A small number
    of host-level views (currently Space session telemetry) intentionally
    aggregate several locally installed runtimes at once. Those callers use
    this discovery helper and still load every module through the same seam.

    A provider does not need an ``adapter.py``. This permits read-only,
    telemetry-only integrations without falsely advertising a complete Plane-B
    chat backend. A capability may be a module (``<capability>.py``) or a
    package (``<capability>/__init__.py``, e.g. storm's ``runtime``).
    """
    if not capability.isidentifier():
        raise ValueError(f"invalid capability name: {capability!r}")
    if not _ADAPTERS_DIR.is_dir():
        return []
    return sorted(
        entry.name
        for entry in _ADAPTERS_DIR.iterdir()
        if entry.is_dir()
        and entry.name.isidentifier()
        and ((entry / f"{capability}.py").is_file() or (entry / capability / "__init__.py").is_file())
    )
