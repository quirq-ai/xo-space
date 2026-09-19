"""Compatibility alias: this router moved to ``modules/connectors/routers/composio_mcp_proxy.py``;
``routers.cowork_agent.connectors.composio_mcp_proxy`` is that module object."""

from modules.connectors.routers import composio_mcp_proxy as _moved

import sys; sys.modules[__name__] = _moved  # noqa: E702
