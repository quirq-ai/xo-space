"""``GET /api/sharing/stream/events``: the relay's transitions as they land.

One feed over ``sharing/events.jsonl`` (``status.py`` writes it); ``since``
replays what arrived after that stamp, ``types`` narrows to the
``sharing.<kind>`` types in ``events.TYPES``.
"""

from __future__ import annotations

from . import store


def events(since=None, types=None):
    return store.events_log().follow(since=since, types=types)


STREAMS = {"events": events}
