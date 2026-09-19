"""Moved to ``modules/sessions/routes.py``; this path resolves to that module."""
from modules.sessions import routes as _moved
import sys; sys.modules[__name__] = _moved
