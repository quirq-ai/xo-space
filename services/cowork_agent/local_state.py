"""Moved to services.storage.paths; this import path stays valid."""
import sys
from services.storage import paths as _moved
sys.modules[__name__] = _moved
