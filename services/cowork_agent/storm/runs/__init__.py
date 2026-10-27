"""Storm's sub-agent runs (design ch 14): one tree per session, captured from
the runtime's run events and reconciled with what its CLI keeps on disk.

* ``records``: the ``runs`` rows and the ``run.*`` events, as SQL run inside
  the engine's one transaction per change (and boot recovery's);
* ``live``: one process's runs in memory (their progress counters, the
  aggregate the stream sends at most once a second);
* ``operations``: list, read (with ancestry), the transcript, stop one, and
  the per-session counts the conversation layer shows.

The engine imports ``records`` and ``live``; ``operations`` imports the
engine, so nothing is re-exported here.
"""
