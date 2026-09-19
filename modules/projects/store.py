"""What the projects module writes, in two tiers.

Committed, inside every project (``<project>/.xo/``, travels through git;
the sample is ``tests/fixtures/xo-project/.xo/``):

* ``project.json``    identity: pid, name, owner, created_at, display name
                      (``xo_structure`` through the identity sink)
* ``todos.json``      session-scoped todos           (``todos_store``)
* ``workitems.json``  durable work items             (``workitems_store``)
* ``peers.json``      who the project is shared with (``peers_store``)

Machine-local, in the project's runtime home under the state root
(``~/.quirq/projects/<pid>/``, shared with the timeline and the sessions
modules, which declare their own files there):

* ``github/issues.json``     the GitHub issue mirror the poller writes
                             (``github_mirror``); a copy of GitHub's state
* ``workitems/claims.json``  which session is working which workitem
                             (``workitem_claims``); the derived in_progress

``stats.json`` and ``sessions/`` under the same folder belong to the
sessions and telemetry modules, not to this one. The paths themselves are
resolved by each store through ``services.cowork_agent.project_layout``.
"""

from __future__ import annotations

from services.storage.files import File

#: Every file this module writes (services/storage/files.py).
FILES = [
    File("project.json", role="record", tier="committed", schema="project",
         note="the project's identity: pid, name, owner, created_at, display name"),
    File("todos.json", role="record", tier="committed", schema="todos",
         note="session-scoped todos, written through the todo API"),
    File("workitems.json", role="record", tier="committed", schema="workitems",
         note="durable work items, local and adopted from GitHub"),
    File("peers.json", role="record", tier="committed", schema="peers",
         note="who the project is shared with"),
    File("projects/<pid>/github/issues.json", role="fact", schema="github-issues",
         note="the GitHub issue mirror the poller keeps; fetched again"),
    File("projects/<pid>/workitems/claims.json", role="record",
         note="which session is working which workitem; in_progress is derived from it"),
]
