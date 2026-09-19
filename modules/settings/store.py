"""The settings module's files: ``~/.quirq/settings/`` and ``~/.quirq/secrets/``.

::

    settings/runtime.env      the runtime settings (agent, watcher), read at startup  (owner-only .env)
    settings/roots.env        the saved roots, read at startup                        (owner-only .env)
    settings/onboarding.json  whether the first-run flow was completed, and when      (Document)
    settings/modules.json     the module switches; written by the kernel              (Document, services/modules.py)
    secrets/secrets.env       the write-only secret store, one KEY=value per line     (owner-only .env)
    secrets/token.json        connector tokens by provider                            (owner-only)

The two ``.env`` files under ``settings/`` are written by ``runtime_config``
with its private atomic writer: the :class:`Document` primitive is for JSON
documents, and an env file carries no schema stamp. ``onboarding.json`` is
a :class:`Document` (absent reads as empty, a file that is not JSON is
refused with a 409 that names the document and is never rewritten, unknown
keys survive). ``modules.json`` belongs to the kernel (``services/modules.py``
reads and writes it) and is declared here because it lives in this module's
folder. Under ``secrets/``, ``secrets.env`` is the active agent's env file
where ``QUIRQ_SECRETS_FILE`` places it (``registry/agent_env.py`` writes it
owner-only) and ``token.json`` holds the connector tokens
(``connectors/token_store.py``); both are credentials, kept by uninstall.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from services.storage.document import Document
from services.storage.files import File
from services.storage.layout import secrets_dir, settings_dir

#: On-disk revision of ``onboarding.json``.
ONBOARDING_SCHEMA = 1

#: Every file this module writes or owns (services/storage/files.py): the
#: layout test, the fixture README and the "delete it and you lose" column
#: derive from this table.
FILES = [
    File("settings/runtime.env", role="decision",
         note="the runtime settings: the agent for new chats and the watcher; read at startup"),
    File("settings/roots.env", role="decision",
         note="the saved projects root and state root; read at startup"),
    File("settings/onboarding.json", role="decision", schema="onboarding",
         note="whether the first-run flow was completed, and when"),
    File("settings/modules.json", role="decision", schema="modules",
         note="the module switches; written by the kernel"),
    File("secrets/secrets.env", role="secret",
         note="the write-only secret store, one KEY=value per line; the active agent's env"),
    File("secrets/token.json", role="secret",
         note="connector tokens by provider"),
]


# ── Paths ────────────────────────────────────────────────────────────────────


def settings_folder() -> Path:
    """``<state root>/settings/``: Space-wide choices."""
    return settings_dir()


def secrets_folder() -> Path:
    """``<state root>/secrets/``: credentials, owner-only."""
    return secrets_dir()


def onboarding_file() -> Path:
    return settings_folder() / "onboarding.json"


def modules_file() -> Path:
    """The kernel's switch file; ``services.modules.settings_path`` says the same."""
    return settings_folder() / "modules.json"


def token_file() -> Path:
    return secrets_folder() / "token.json"


# The env files honour an override (``QUIRQ_RUNTIME_FILE``, ``QUIRQ_SECRETS_FILE``),
# so their writers name them: ``runtime_config.runtime_config_file()``,
# ``runtime_config.root_config_file()`` and ``registry.agent_env.ENV_FILE``.


# ── Handles ──────────────────────────────────────────────────────────────────


def onboarding_document(path: Optional[Path] = None) -> Document:
    """The onboarding document. ``path`` lets ``xo_cowork_state`` keep its
    import-time ``STATE_FILE`` (which tests point elsewhere); the default is
    :func:`onboarding_file`."""
    return Document(path if path is not None else onboarding_file(), schema=ONBOARDING_SCHEMA,
                    empty=dict, name="onboarding.json")
