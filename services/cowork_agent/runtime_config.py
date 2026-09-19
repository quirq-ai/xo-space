"""Moved to ``modules/settings/runtime_config.py``; this path resolves to that module."""
from modules.settings import runtime_config as _moved
import sys; sys.modules[__name__] = _moved
