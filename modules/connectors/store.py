"""What the connectors module writes under the state root: nothing.

Every credential a connector keeps lives elsewhere on purpose, so this
module declares no :class:`services.storage.files.File`:

* ``secrets/token.json`` (``token_store``: the GitHub token, the Vercel token
  and client registration) is the settings module's file; this module reads
  and writes entries through ``token_store`` and does not declare the file
  a second time.
* the Composio stores (``api_key.json``, ``sessions.json``,
  ``action_prefs.json``, ``space_scope.json``) live under
  ``~/.config/composio/`` (``composio/paths.py``, ``COMPOSIO_STORE_DIR``),
  outside the state root, kept by uninstall.
* rclone's ``rclone.conf`` is rclone's own, under ``~/.config/rclone/``.
* MagicPath's session is the CLI's, ``~/.magicpath/session.json``.

``FILES`` is therefore empty and the sample root holds no ``connectors/``
folder (``folder`` is ``null`` in ``module.json``).
"""

from __future__ import annotations

from services.storage.files import File

#: Every file this module writes under the state root (services/storage/files.py): none.
FILES: list[File] = []
