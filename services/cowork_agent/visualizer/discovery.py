"""Shared session-row discovery for adapter visualizer sources.

The watcher owns the per-project session index: where each project lives,
what shape the index has, how to iterate every project on disk. Source
modules that ship under
``services/cowork_agent/adapters/<name>/visualizer_source.py`` should
**not** re-implement that walk — they just consume the tuples this
module yields and do the backend-specific work (path translation,
SQLite query, JSONL tail, etc.) on the rows that match their name.

Keeping this in shared code instead of duplicating it per adapter
means a new agent's ``visualizer_source.py`` only needs to know about
its **own** transcript/state storage. Watcher infrastructure stays in
the watcher.
"""

from __future__ import annotations

from typing import Iterator

from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.visualizer.workspace_index import list_project_ids


def iter_sessionslist_rows(backend: str) -> Iterator[tuple[str, str, dict]]:
    """Yield ``(project_id, composite_key, row)`` for every adapter row
    in any project's session index whose ``backend`` field equals
    ``backend``.

    Order: by project id (whatever ``list_project_ids`` returns), then
    by the merged index's iteration order within each project — which
    since T19 is the shard file order. Sources that need a deterministic
    order should sort themselves.

    The ``list_project_ids()`` call here is one of the eight a single
    watcher tick used to make (docs/syncplan.md §10, T23). It needs no
    argument threading: the watcher wraps its tick in
    ``workspace_index.project_index_scope()``, so this call — made from
    an adapter source the watcher drives — is served from that tick's
    memo. Called from a request thread it walks the root, as before.

    Skips quietly on a missing or unreadable index, a malformed shard, a
    row whose value isn't a dict, and a row whose ``backend`` doesn't
    match (the source-name filter) — see
    ``engine.sessions_io.read_session_index``.

    **Ordering.** This runs inside ``Source.poll_events()``, which the
    watcher drains at the very top of a tick — BEFORE the identity sink
    has minted a pid for a brand-new project. Runtime resolution therefore
    has to tolerate a project whose runtime home does not exist yet: it
    resolves to nothing and this yields no rows for that project this
    tick, rather than creating a directory the identity sink has not
    agreed exists.

    Each shard is published with an atomic rename, so this reader sees a
    consistent snapshot of every row per call.
    """
    for project_id in list_project_ids():
        for composite_key, row in session_index.read_session_index(project_id).items():
            if row.get("backend") != backend:
                continue
            yield project_id, composite_key, row
