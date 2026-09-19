"""Compatibility alias: the Composio connector moved to ``modules/connectors/composio``.

``services.cowork_agent.connectors.composio`` and each of its modules
(action_prefs, byo_key, categories, client, identity, mcp, paths, service, space_scope) resolve to the moved objects, so an import or a patch
through either path reaches one module. New code imports
``modules.connectors.composio``.
"""

from __future__ import annotations

import sys

from modules.connectors import composio as _moved
from modules.connectors.composio import action_prefs, byo_key, categories, client, identity, mcp, paths, service, space_scope

for _name, _mod in (("action_prefs", action_prefs), ("byo_key", byo_key), ("categories", categories), ("client", client), ("identity", identity), ("mcp", mcp), ("paths", paths), ("service", service), ("space_scope", space_scope)):
    sys.modules[f"{__name__}.{_name}"] = _mod
sys.modules[__name__] = _moved
