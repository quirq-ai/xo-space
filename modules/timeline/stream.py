"""``GET /api/timeline/stream/events``: lines as they land, across the Space.

One feed over every project log and the Space log
(``services.storage.eventlog.follow_many``); each line gains ``project``,
the pid of the log it came from or ``"space"``. ``since`` replays what
landed after that stamp first. A project log created after the stream
opened joins it on the next connection.
"""

from __future__ import annotations

from services.storage.eventlog import follow_many

from . import store


def follow(since=None, types=None):
    logs = {**store.project_logs(), "space": store.space_log()}
    return follow_many(logs, since=since, types=types, tag="project")


STREAMS = {"events": follow}
