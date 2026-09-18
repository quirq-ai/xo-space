"""The Work: everything a person needs to see across the Space, read from
the source logs at read time, with the person's own decisions in one small
folder per page, ``~/.quirq/work/{inbox,live,history}/`` (docs/work-and-workitems.md).

Five modules, one router-facing surface:

* :mod:`store`      the three files, one in memory (normalisation, retention,
                    locked read-modify-write, the marks: dismissed, acked,
                    promoted, pinned, watermark, posts).
* :mod:`readers`    one function per source (the Space timeline, GitHub
                    issue mirrors, connection events, the sharing relay,
                    job runs, agent posts), each answering the one entry
                    shape newest-first; nothing copied.
* :mod:`attention`  what needs a person, derived from current state on
                    every read, keyed ``key@since``.
* :mod:`inbox`      the Inbox page's four groups composed in one read.
* :mod:`service`    what ``routers/cowork_agent/bff/work.py`` imports.

A property of the Space, so a top-level package beside ``inbox`` (which it
replaces) and ``connections``. Core code: names no agent.
"""
