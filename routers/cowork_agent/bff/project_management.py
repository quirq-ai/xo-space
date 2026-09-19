"""Moved to ``modules/projects/routes.py``; this path resolves to that module."""
from modules.projects import routes as _moved
import sys; sys.modules[__name__] = _moved
