"""The agent side as one module.

Nothing moves: ``routes.py`` re-exports the broker routers under
``routers/`` and ``tasks.py`` lists the boot loops that used to be started
by hand in ``server.py``. The capability loader
(``services/cowork_agent/adapters/loader.py``) stays the seam for
agent-specific code; adapters are this module's plug-ins.
"""
