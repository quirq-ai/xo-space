"""Moved to services.storage.flock; this import path stays valid."""
import sys
from services.storage import flock as _moved
sys.modules[__name__] = _moved
