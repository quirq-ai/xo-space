"""Moved to ``modules/projects/routes.py``; this path resolves to that module
(``router``, ``project_tree``, ``project_file``, ``FileHistoryResponse`` and
the other names live there)."""
from modules.projects import routes as _moved
import sys; sys.modules[__name__] = _moved
