"""What the timeline module emits and raises.

The 21 legacy event types (``session.started``, ``todo.added``,
``workitem.claimed`` and friends) are declared by the schema's ``type`` enum
(``services/cowork_agent/visualizer/schema/timeline.schema.json``); a
module that emits a new type names it in its own ``events.TYPES`` and
:func:`modules.timeline.service.declared_types` unions both.
"""

TYPES = ()

SIGNALS = ()
