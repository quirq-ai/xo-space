"""Compatibility alias: the OneDrive connector moved to ``modules/connectors/onedrive``.

``services.cowork_agent.connectors.onedrive`` and each of its modules
(provider) resolve to the moved objects, so an import or a patch
through either path reaches one module. New code imports
``modules.connectors.onedrive``.
"""

from __future__ import annotations

import sys

from modules.connectors import onedrive as _moved
from modules.connectors.onedrive import provider

for _name, _mod in (("provider", provider),):
    sys.modules[f"{__name__}.{_name}"] = _mod
sys.modules[__name__] = _moved
