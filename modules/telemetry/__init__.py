"""Telemetry: what the agents spent and did.

Usage (tokens, messages, tools and models, per session, per project and
across the workspace), the session telemetry every installed runtime
reports (``/xo/sessions.json``) and where each of those sources is read
from (``/api/telemetry/sources``). The watcher that ingests the agents'
session stores into ``stats.json``, ``sessions-augment.json``, the
presence snapshots and the heartbeat is this module's task, and so is the
daily usage report to XO.

The contract files (``modules/telemetry``):

* :mod:`store`         the files: the usage watermark under ``usage/``, the
                       per-project stats and session counts under
                       ``projects/<pid>/``, the watcher's cursors beside them,
                       and the rebuilt rollups under ``cache/``.
* :mod:`models`        the usage wire models (the response models the
                       visualizer routers used to hold).
* :mod:`presenter`     pure ``stats.json`` to wire-shape transforms, shared
                       by the project and the workspace tier.
* :mod:`sources`       the telemetry source descriptors and their switches
                       (was ``services/telemetry_sources.py``).
* :mod:`usage_loader`  the active agent's ``usage`` capability, through the
                       loader (was ``services/cowork_agent/engine/usage_loader.py``).
* :mod:`usage_sync`    the daily usage report to XO and its watermark (was
                       ``services/usage_sync.py``).
* :mod:`service`       the only surface ``routes.py``, ``tasks.py``,
                       ``commands.py`` and other modules call.
* :mod:`routes`        ``/api/usage/*``, ``/api/xo-projects/usage/*``,
                       ``/api/xo-projects/{id}/usage/*``, ``/api/telemetry/*``
                       and ``/xo/sessions.json``; ``tasks`` the watcher and the
                       usage sync; ``commands`` ``python -m quirq telemetry ...``;
                       ``pages/sources.json`` the Agents tab's Configure page.

The watcher itself, its sinks and the workspace builders stay under
``services/cowork_agent/visualizer/``: they are the agent-side ingestion
and reach adapter capabilities through the loader. Core code: names no
agent and imports nothing from the adapters tree. The old import paths
(``services.telemetry_sources``, ``services.usage_sync``,
``services.cowork_agent.engine.usage_loader``) are aliases of the moved
modules for one release.
"""
