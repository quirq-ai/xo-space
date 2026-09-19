"""Moved to ``modules/settings/routes.py`` (the settings module's router, mounted by the registry); this path resolves to that module."""
from modules.settings import routes as _moved
import sys; sys.modules[__name__] = _moved
