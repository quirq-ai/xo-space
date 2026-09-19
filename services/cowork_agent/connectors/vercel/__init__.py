"""Compatibility alias: the Vercel connector moved to ``modules/connectors/vercel``.

``services.cowork_agent.connectors.vercel`` and each of its modules
(api, connector, oauth) resolve to the moved objects, so an import or a patch
through either path reaches one module. New code imports
``modules.connectors.vercel``.
"""

from __future__ import annotations

import sys

from modules.connectors import vercel as _moved
from modules.connectors.vercel import api, connector, oauth

for _name, _mod in (("api", api), ("connector", connector), ("oauth", oauth)):
    sys.modules[f"{__name__}.{_name}"] = _mod
sys.modules[__name__] = _moved
