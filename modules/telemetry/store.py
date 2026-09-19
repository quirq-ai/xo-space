"""The telemetry module's files, in three shared folders of the state root.

::

    usage/<agent>.json                            how far usage was reported to XO (fact)
    projects/<pid>/stats.json                     one project's rolling totals, by session and by day
    projects/<pid>/sessions/sessions-augment.json one project's message, tool and task counts per session
    projects/offsets.json                         where the watcher stopped reading each session file
    projects/<source>-offsets.json                an adapter source's own cursor, beside the shared one
    cache/stats.json                              the workspace total of every project's stats.json
    cache/sessions/sessionslist.json              every project's session index, one map
    cache/sessions/sessions-augment.json          every project's session counts, one map
    cache/heartbeat.json                          the watcher's once-per-tick liveness beat
    cache/activity/workspace.json                 which sessions are open right now, across every project
    cache/activity/projects/<project>.json        which sessions are open right now in one project

The module's own folder is ``usage/`` (the watermark the daily report
advances, one file per agent). Everything else is derived by the watcher
(``services/cowork_agent/visualizer/``: the sinks write the per-project
files, the workspace builders the rollups under ``cache/``) and is rebuilt
on the next tick when deleted. The cursors sit beside the history they
count, so deleting ``projects/`` resets both together and nothing is
replayed onto surviving totals. The paths are resolved here through the
layout and the watcher's state module; no file name is spelled twice.
"""

from __future__ import annotations

from pathlib import Path

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer import state as watcher_state
from services.storage.files import File
from services.storage.layout import usage_dir

#: Every file this module writes (services/storage/files.py): the layout
#: test, the fixture README and the "delete it and you lose" column derive
#: from this table.
FILES = [
    File("usage/<agent>.json", role="fact",
         note="how far usage was reported to XO, per agent, with the last key probe"),
    File("projects/<pid>/stats.json", role="cache", schema="stats",
         note="one project's rolling token, tool and model totals, by session and by day"),
    File("projects/<pid>/sessions/sessions-augment.json", role="cache", schema="sessions-augment",
         note="one project's message, tool and task counts per session"),
    File("projects/offsets.json", role="cache",
         note="where the watcher stopped reading; a cursor beside the history it counts"),
    File("projects/<source>-offsets.json", role="cache",
         note="where the watcher stopped reading; a cursor beside the history it counts, one per adapter source that keeps its own"),
    File("cache/stats.json", role="cache", schema="stats",
         note="the workspace total of every project's stats.json"),
    File("cache/sessions/sessionslist.json", role="cache", schema="sessionslist",
         note="every project's session index, one map"),
    File("cache/sessions/sessions-augment.json", role="cache", schema="sessions-augment",
         note="every project's session counts, one map"),
    File("cache/heartbeat.json", role="cache",
         note="the watcher's once-per-tick liveness beat"),
    File("cache/activity/workspace.json", role="cache", schema="activity",
         note="which sessions are open right now, across every project"),
    File("cache/activity/projects/<project>.json", role="cache", schema="activity",
         note="which sessions are open right now in one project"),
]


# ── Paths ────────────────────────────────────────────────────────────────────


def watermark_path(agent: str) -> Path:
    """``usage/<agent>.json``: how far usage was reported for one agent."""
    return usage_dir() / f"{agent}.json"


def project_stats_path(pid: str) -> Path:
    return project_layout.runtime_dir(pid) / "stats.json"


def project_sessions_augment_path(pid: str) -> Path:
    return project_layout.runtime_dir(pid) / "sessions" / "sessions-augment.json"


def offsets_path() -> Path:
    """The shared cursor of the jsonl sources (``projects/offsets.json``)."""
    return watcher_state.watcher_state_dir() / "offsets.json"


def workspace_stats_path() -> Path:
    return project_layout.workspace_runtime_dir() / "stats.json"


def workspace_sessionslist_path() -> Path:
    return project_layout.workspace_sessions_dir() / "sessionslist.json"


def workspace_sessions_augment_path() -> Path:
    return project_layout.workspace_sessions_dir() / "sessions-augment.json"


def heartbeat_path() -> Path:
    return watcher_state.watcher_heartbeat_path()


def workspace_activity_path() -> Path:
    return watcher_state.workspace_activity_path()


def project_activity_path(project_id: str) -> Path:
    return watcher_state.project_activity_path(project_id)
