"""Compatibility alias: this router moved to ``modules/connectors/routers/magicpath.py``;
``routers.cowork_agent.connectors.magicpath`` is that module object."""

from modules.connectors.routers import magicpath as _moved

import sys; sys.modules[__name__] = _moved  # noqa: E702
