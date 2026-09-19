"""Moved to ``modules/projects/project_management.py``; this path resolves to that module."""
from modules.projects import project_management as _moved
import sys; sys.modules[__name__] = _moved
