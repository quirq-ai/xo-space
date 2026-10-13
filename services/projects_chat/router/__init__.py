"""The router: which provider decides where a message goes, and the one shape it returns.

``mode.py`` reads the switches; ``decision.py`` defines :class:`RouterRequest`
and :class:`Decision`, the same for every provider (the agent coordinator, a
decision model, or none when the router is off).
"""
