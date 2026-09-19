"""``GET /api/connections/stream/events``: collected events as they land.

One merged feed over every configured toolkit's ``events.jsonl`` (each
line gains ``toolkit``); ``since`` replays what arrived after that stamp.
"""

from __future__ import annotations

from services.storage.eventlog import follow_many

from . import store


def events(since=None, types=None):
    logs = {toolkit: store.events_log(toolkit) for toolkit in store.list_configured()}
    return follow_many(logs, since=since, types=types, tag="toolkit")


STREAMS = {"events": events}
