"""Compatibility alias: this router moved to ``modules/connectors/routers/onedrive.py``;
``routers.cowork_agent.connectors.onedrive`` is that module object."""

from modules.connectors.routers import onedrive as _moved

import sys; sys.modules[__name__] = _moved  # noqa: E702
