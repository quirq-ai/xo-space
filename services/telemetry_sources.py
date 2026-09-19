"""Compatibility alias: the telemetry sources moved to ``modules/telemetry/sources.py``.

``services.telemetry_sources`` resolves to the same module object as
``modules.telemetry.sources``, so an import or a patch through either path
reaches one object. New code calls ``modules.telemetry.service``.
"""

from modules.telemetry import sources as _moved
import sys; sys.modules[__name__] = _moved  # noqa: E702
