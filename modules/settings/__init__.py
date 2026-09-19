"""Settings: Space-wide choices, and the files under ``~/.quirq/settings/``
and ``~/.quirq/secrets/`` that hold them.

The contract files of the module (``modules/settings``):

* :mod:`runtime_config`  the runtime settings (``runtime.env``: the agent for
                         new chats, the watcher), the saved roots
                         (``roots.env``), the restart state and the Setup
                         status payload; moved here from
                         ``services/cowork_agent/runtime_config.py``.
* :mod:`xo_cowork_state` onboarding state on ``settings/onboarding.json``, a
                         :class:`services.storage.document.Document`; moved
                         from ``services/cowork_agent/xo_cowork_state.py``.
* :mod:`setup_status`    the read-only identity checks behind
                         ``GET /space/setup/status`` (the route stays in
                         ``routers/space.py`` and calls the facade); moved
                         from ``services/setup_status.py``.
* :mod:`store`           the file table (``FILES``): the two env files, the
                         onboarding document, the kernel's ``modules.json``,
                         and the credentials under ``secrets/``.
* :mod:`service`         the only surface ``routes.py``, ``commands.py`` and
                         other modules call: effective and saved settings,
                         ``save_settings``, the roots, the restart state,
                         :class:`SecretsScope` and the curated secret reads
                         and writes, onboarding, the setup status snapshot.
* :mod:`routes`          ``/api/runtime-config``, ``/api/secrets`` (the
                         curated and the legacy whole-file routes) and
                         ``/api/onboarding``, the manifest's aliases.
* :mod:`commands`        ``python -m quirq settings settings|modules``.
* ``pages/``             the Workspace page (the runtime settings and roots
                         over ``/api/runtime-config``) and the Modules page
                         (the switch table over the kernel's ``/api/modules``).

The old import paths (``services.cowork_agent.runtime_config``,
``services.cowork_agent.xo_cowork_state``, ``services.setup_status``, the
four routers under ``routers/cowork_agent/``) resolve to these same modules;
``services.cowork_agent.scopes`` re-exports :class:`SecretsScope`.
"""
