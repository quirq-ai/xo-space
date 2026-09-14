"""Moved to services.storage.atomic_write; this import path stays valid."""
import sys
from services.storage import atomic_write as _moved
sys.modules[__name__] = _moved
