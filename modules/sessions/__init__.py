"""Sessions: every agent session the Space knows.

A session is one conversation with an agent. The Space keeps, per project,
one index row for it (the adapter's shard under
``projects/<pid>/sessions/sessionslist.d/``: ``sessionId``,
``nativeSessionId``, ``directory``, ``backend``, ``updatedAt``, ``usage``),
and this module adds the one field the adapters do not know: ``purpose``,
why the session exists (``chat``, ``inbox_item``, ``job``, ``workitem`` or
any short word). The transcript itself stays in the agent's own store and
is read through the owning adapter's ``sessions`` capability.

The contract files of the module (``modules/sessions``):

* :mod:`sessions_io`          the per-project index (shards, the merged
                              read, the row lookup by session id), the
                              listing every route builds on and the
                              backend resolution; moved from
                              ``services/cowork_agent/engine/sessions_io.py``,
                              which still resolves to it.
* :mod:`session_transcript`   the chat-style projection of a session's
                              record (one bubble per turn); moved from
                              ``services/cowork_agent/session_transcript.py``.
* :mod:`service`              the facade: the reads and writes behind the
                              routes, ``record_purpose`` and ``start``, which
                              runs the active agent's stream for a purpose,
                              stamps the purpose on the index row and emits
                              ``session.started`` once through
                              ``modules.timeline``.
* :mod:`routes`               ``/api/sessions``, ``/api/messages`` and
                              ``/api/chat`` (the chat prompt, its
                              server-sent event stream, abort and respond);
                              ``routers/cowork_agent/sessions.py`` and
                              ``chat.py`` still resolve to it.
* :mod:`stream`               ``/api/sessions/stream/events``: the session
                              lifecycle lines of every project timeline.
* :mod:`commands`             ``python -m quirq sessions list|get``.
* :mod:`events`               ``session.started`` and ``session.closed``,
                              the ``started`` and ``closed`` signals.
* :mod:`store`                the file table (the index shard).
* ``pages/list.json``         the Agents tab's Sessions page.

Core code: names no agent. The backend is whatever ``resolve_agent_name``
or the session's own index row says, and everything agent-specific
(``enrich_project_session``, ``resolve_native_file``, ``get_messages``,
``set_session_directory``, ``handle_prompt``) is reached through
``services.cowork_agent.adapters.loader``. The agent engine it drives
(``services/cowork_agent/engine/dispatcher.py``, ``chat_state.py``,
``messages.py``) stays where it is.
"""
