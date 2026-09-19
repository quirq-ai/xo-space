"""Compatibility alias: this router moved to ``modules/connectors/routers/github_cli.py``;
``routers.cowork_agent.connectors.github_cli`` is that module object."""

from modules.connectors.routers import github_cli as _moved

import sys; sys.modules[__name__] = _moved  # noqa: E702
