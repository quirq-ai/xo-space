"""Compatibility aliases: the connector routers moved to
``modules/connectors/routers/``. Each module here resolves to the moved one
(the ``sys.modules`` form), so ``routers.cowork_agent.all_routers`` keeps
importing them until the registry sweep drops these entries; the same
routers are mounted through ``modules/connectors/routes.py``.
"""
