"""Moved to ``modules/projects/todos_store.py``; this path resolves to that module."""
from modules.projects import todos_store as _moved
import sys; sys.modules[__name__] = _moved
