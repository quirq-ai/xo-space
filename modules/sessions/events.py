"""What the sessions module emits and raises.

``session.started`` is written once per :func:`modules.sessions.service.start`
to the project's timeline (through ``modules.timeline.service.emit``): the
line carries ``session_id`` (the Space session id the API serves),
``runtime`` (the backend) and ``purpose``. ``session.closed`` is the
lifecycle's other end, reserved for an explicit close (nothing emits it
yet; a turn ending is not a session closing). Both were legacy types in the
timeline schema's ``type`` enum; declaring them here makes this module
their owner without touching that enum.

Signals go through ``services.signals``: ``sessions.started`` fires after
the timeline line is written, with ``session_id``, ``purpose``,
``project_id`` and ``runtime``; ``sessions.closed`` is reserved the same
way as the event.
"""

TYPES = ("session.started", "session.closed")

SIGNALS = ("started", "closed")
