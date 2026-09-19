"""Compatibility alias: the commit relay moved to ``modules/sharing``.

``services.cowork_agent.project_sharing.<name>`` resolves to the same module
object as ``modules.sharing.<name>`` (clone, config, git_ops, poller,
repo_identity, service, state, status, watcher), so an import or a patch
through either path reaches one object. ``log_line`` is re-exported the
same way. New code imports ``modules.sharing.service``.
"""

from __future__ import annotations

import sys

from modules.sharing import (  # noqa: F401  (log_line is re-exported)
    clone, config, git_ops, log_line, poller, repo_identity, service, state, status, watcher,
)

for _name, _mod in (("clone", clone), ("config", config), ("git_ops", git_ops), ("poller", poller),
                    ("repo_identity", repo_identity), ("service", service), ("state", state),
                    ("status", status), ("watcher", watcher)):
    sys.modules[f"{__name__}.{_name}"] = _mod
