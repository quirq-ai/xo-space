"""What the connections module emits and raises."""

#: Event lines this module writes to its own log (``events.jsonl`` holds
#: collector rows, whose ``type`` is the collector id, not a timeline type).
TYPES = ()

#: Signals raised through ``services.signals``: ``connections.new_events``
#: fires after a poll that collected something, with ``toolkit=``.
SIGNALS = ("new_events",)
