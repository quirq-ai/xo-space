"""Moved to services.storage.reader; this import path stays valid."""
import sys
from services.storage import reader as _moved
sys.modules[__name__] = _moved
