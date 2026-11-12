"""The runtimes that can read their account's usage with no model call.

Discovered, never named: every adapter that ships a runtime (capability
``runtime``, the adapter contract) and lists the ``usage_read`` feature as native
(contract 1.1, ``UsageReader``). Adding an agent with such a read needs no
change here.
"""

from __future__ import annotations

import logging
from typing import Mapping

from services.cowork_agent.adapters.contract import UsageReader
from services.cowork_agent.adapters.loader import list_capability_providers, try_load_capability

log = logging.getLogger(__name__)

FEATURE = "usage_read"


def usage_readers() -> Mapping[str, UsageReader]:
    """Each such runtime, by agent. One whose module fails to load is left out
    and logged: its usage stays what its turns report."""
    found: dict[str, UsageReader] = {}
    for agent in list_capability_providers("runtime"):
        try:
            module = try_load_capability("runtime", agent=agent)
            runtime = module.runtime() if module is not None else None
            feature = runtime.features().features.get(FEATURE) if runtime is not None else None
        except Exception:  # noqa: BLE001 - one broken adapter must not hide the others' reads
            log.exception("advisory: %s's runtime couldn't be loaded; its usage isn't read", agent)
            continue
        if feature is not None and feature.support == "native" and callable(getattr(runtime, "read_usage", None)):
            found[agent] = runtime
    return found
