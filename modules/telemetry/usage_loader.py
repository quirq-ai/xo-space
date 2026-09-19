"""The active agent's usage module, through the capability loader.

The active agent is resolved from ``AGENT_NAME`` (or ``DEFAULT_AGENT``) via
``get_active_agent()``. The corresponding usage module is imported at
``services.cowork_agent.adapters.<name>.usage``, alongside the other
agent-specific Python (adapter.py, channels_status.py, providers_status.py,
models_status.py, streaming.py, transcript.py). Adding a new agent is
"drop services/cowork_agent/adapters/<name>/usage.py" with no other code
changes.

No if/elif dispatcher. Single resolver, single source of truth. Was
``services/cowork_agent/engine/usage_loader.py``; that path is an alias.
"""
from __future__ import annotations

from types import ModuleType

from services.cowork_agent.adapters.loader import load_capability


def load_usage_module() -> ModuleType:
    """Return the active agent's usage module.

    Thin alias over :func:`services.cowork_agent.adapters.loader.load_capability`
    (``capability="usage"``), kept for the existing call sites.

    Raises:
        ModuleNotFoundError: if the active agent has no
            ``services/cowork_agent/adapters/<name>/usage.py``. The error
            names the path we tried to import so the fix is obvious.
    """
    return load_capability("usage")
