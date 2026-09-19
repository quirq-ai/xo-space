"""Compatibility alias: this router moved to ``modules/connectors/routers/vercel.py``;
``routers.cowork_agent.connectors.vercel`` is that module object."""

from modules.connectors.routers import vercel as _moved

import sys; sys.modules[__name__] = _moved  # noqa: E702
