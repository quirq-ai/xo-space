"""Compatibility alias: the GitHub connector moved to ``modules/connectors/github``.

``services.cowork_agent.connectors.github`` and each of its modules
(cli_auth, common, issue_actions, issues, pat) resolve to the moved objects, so an import or a patch
through either path reaches one module. New code imports
``modules.connectors.github``.
"""

from __future__ import annotations

import sys

from modules.connectors import github as _moved
from modules.connectors.github import cli_auth, common, issue_actions, issues, pat

for _name, _mod in (("cli_auth", cli_auth), ("common", common), ("issue_actions", issue_actions), ("issues", issues), ("pat", pat)):
    sys.modules[f"{__name__}.{_name}"] = _mod
sys.modules[__name__] = _moved
