"""Compatibility alias: the rclone engine moved to ``modules/connectors/rclone``.

``services.cowork_agent.connectors.rclone`` and each of its modules
(connector, oauth_lock) resolve to the moved objects, so an import or a patch
through either path reaches one module. New code imports
``modules.connectors.rclone``.
"""

from __future__ import annotations

import sys

from modules.connectors import rclone as _moved
from modules.connectors.rclone import connector, oauth_lock

for _name, _mod in (("connector", connector), ("oauth_lock", oauth_lock)):
    sys.modules[f"{__name__}.{_name}"] = _mod
sys.modules[__name__] = _moved
