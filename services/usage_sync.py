"""Compatibility alias: the usage sync moved to ``modules/telemetry/usage_sync.py``.

``services.usage_sync`` resolves to the same module object as
``modules.telemetry.usage_sync``, so an import or a patch through either
path reaches one object. New code calls ``modules.telemetry.service``.
"""

from modules.telemetry import usage_sync as _moved
import sys; sys.modules[__name__] = _moved  # noqa: E702
