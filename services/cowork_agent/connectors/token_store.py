"""Compatibility alias: ``token_store`` moved to ``modules/connectors/token_store.py``.

``services.cowork_agent.connectors.token_store`` is that module object, so an
import or a patch through either path reaches one module.
"""

from modules.connectors import token_store as _moved

import sys; sys.modules[__name__] = _moved  # noqa: E702
