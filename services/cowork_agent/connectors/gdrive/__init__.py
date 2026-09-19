"""Compatibility alias: the Google Drive connector moved to ``modules/connectors/gdrive``.

``services.cowork_agent.connectors.gdrive`` and each of its modules
(provider) resolve to the moved objects, so an import or a patch
through either path reaches one module. New code imports
``modules.connectors.gdrive``.
"""

from __future__ import annotations

import sys

from modules.connectors import gdrive as _moved
from modules.connectors.gdrive import provider

for _name, _mod in (("provider", provider),):
    sys.modules[f"{__name__}.{_name}"] = _mod
sys.modules[__name__] = _moved
