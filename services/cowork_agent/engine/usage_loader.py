"""Compatibility alias: the usage loader moved to ``modules/telemetry/usage_loader.py``.

``services.cowork_agent.engine.usage_loader`` resolves to the same module
object as ``modules.telemetry.usage_loader``, so an import or a patch
through either path reaches one object.
"""

from modules.telemetry import usage_loader as _moved
import sys; sys.modules[__name__] = _moved  # noqa: E702
