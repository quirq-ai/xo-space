"""File primitives every part of a Space builds on.

Space-level: usable by the people-facing packages (``services/inbox``,
``services/connections``) and by the agent tree (``services/cowork_agent``)
alike, so nothing generic about files lives under either.

* :mod:`flock`         ``locked(path)``: the advisory lock around one
                       read-modify-write of a shared file.
* :mod:`atomic_write`  ``write_json_atomic``, ``append_jsonl``,
                       ``write_json_owned``, ``write_json_atomic_if_changed``,
                       ``ChangeGate`` and the stamped-document readers.
* :mod:`reader`        ``read_json``, ``read_jsonl_tail_reverse`` and the
                       session-record merge.
* :mod:`paths`         ``quirq_state_dir`` and ``legacy_state_dir``: the
                       machine-local state roots.

The former paths (``services.cowork_agent.visualizer.flock``,
``.atomic_write``, ``.reader`` and ``services.cowork_agent.local_state``)
alias these modules in ``sys.modules``, so an import or a patch through
either path reaches the same object. Nothing is re-exported here on purpose:
import the module you need.
"""
