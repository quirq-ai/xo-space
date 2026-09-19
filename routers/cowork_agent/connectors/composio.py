"""Compatibility alias: this router moved to ``modules/connectors/routers/composio.py``;
``routers.cowork_agent.connectors.composio`` is that module object."""

from modules.connectors.routers import composio as _moved

import sys; sys.modules[__name__] = _moved  # noqa: E702
