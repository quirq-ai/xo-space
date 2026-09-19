"""Compatibility alias: the connections package moved to ``modules/connections``.

``services.connections.<name>`` resolves to the same module object as
``modules.connections.<name>`` (store, collectors, mcp_client, poller,
service), so an import or a patch through either path reaches one object.
New code imports ``modules.connections.service``.
"""

from __future__ import annotations

import sys

from modules.connections import collectors, mcp_client, poller, service, store

for _name, _mod in (("store", store), ("collectors", collectors), ("mcp_client", mcp_client),
                    ("poller", poller), ("service", service)):
    sys.modules[f"{__name__}.{_name}"] = _mod
