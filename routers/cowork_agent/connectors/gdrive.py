"""Compatibility alias: this router moved to ``modules/connectors/routers/gdrive.py``;
``routers.cowork_agent.connectors.gdrive`` is that module object."""

from modules.connectors.routers import gdrive as _moved

import sys; sys.modules[__name__] = _moved  # noqa: E702
