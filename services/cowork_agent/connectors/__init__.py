"""Compatibility alias: the connectors moved to ``modules/connectors``.

Every name under this package resolves to the moved module object:
``services.cowork_agent.connectors.token_store`` is
``modules.connectors.token_store``, and each connector package (``composio``,
``gdrive``, ``onedrive``, ``github``, ``vercel``, ``rclone``) and its
submodules alias theirs the same way (the ``sys.modules`` form, one alias
file per moved module), so an import or a patch through either path reaches
one object. The routers moved with them, ``routers/cowork_agent/connectors/``
to ``modules/connectors/routers/``. New code imports ``modules.connectors``.
"""
