"""Compatibility alias: this router moved to ``modules/connectors/routers/github_pat.py``;
``routers.cowork_agent.connectors.github_pat`` is that module object."""

from modules.connectors.routers import github_pat as _moved

import sys; sys.modules[__name__] = _moved  # noqa: E702
