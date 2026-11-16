"""Moved to services.git_safe; this import path stays valid."""
import sys
from services import git_safe as _moved
sys.modules[__name__] = _moved
