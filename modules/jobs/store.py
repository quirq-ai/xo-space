"""The jobs module's files: ``~/.quirq/jobs/`` and its logs.

::

    jobs/jobs.json        definitions, written only by registration      (Document, owner-only)
    jobs/state.json       next_run / last_run / running_since / last_result (Document, owner-only)
    jobs/runs/<id>.jsonl  append-only run history, newest last            (EventLog, 8 MB, keep 3)
    logs/jobs/<id>.log    the executor's own log of every run, with the other logs

The two documents follow the :class:`services.storage.document.Document`
rules: absent reads as empty, a file that is not JSON is refused with a 409
that names the document and never its path, unknown keys survive a rewrite,
and both are owner-only (they can hold a command's environment). The run
history is a :class:`services.storage.eventlog.EventLog`: every line starts
with ``ts`` and ``type`` (``job.run``), and readers see the live file and
the rotations as one log, newest first.

The folder is named here once, below the layout module, so the scheduler
and the layout test agree on it (``services.storage.layout.jobs_dir`` says
the same).
"""

from __future__ import annotations

from pathlib import Path

from services.storage.document import CorruptDocument, Document
from services.storage.eventlog import EventLog
from services.storage.files import File
from services.storage.paths import quirq_state_dir
from utils.runtime_env import logs_dir

SCHEMA = 1
#: The ``type`` of every line in ``runs/<id>.jsonl``; ``status`` is the outcome.
RUN_EVENT_TYPE = "job.run"
#: Tests patch these two; never write 8 MB to exercise rotation.
RUNS_ROTATE_BYTES = 8 << 20
RUNS_KEEP = 3

#: Every file this module writes (services/storage/files.py): the layout
#: test, the fixture README and the "delete it and you lose" column derive
#: from this table. The logs under ``logs/jobs/`` are the shared tier's.
FILES = [
    File("jobs/jobs.json", role="decision", schema="jobs",
         note="saved commands: what runs, how often, with what timeout"),
    File("jobs/state.json", role="fact", schema="jobs-state",
         note="next run, running since, last result"),
    File("jobs/runs/<id>.jsonl", role="record", log=True, rotate="8 MB, keep 3",
         note="one line per run, newest last"),
]


# ── Paths ────────────────────────────────────────────────────────────────────


def jobs_dir() -> Path:
    """``<state root>/jobs/``: saved commands, their state and run history."""
    return quirq_state_dir() / "jobs"


def jobs_file() -> Path:
    return jobs_dir() / "jobs.json"


def state_file() -> Path:
    return jobs_dir() / "state.json"


def runs_file(job_id: str) -> Path:
    return jobs_dir() / "runs" / f"{job_id}.jsonl"


def log_file(job_id: str) -> Path:
    # Logs are safe to delete, so they sit with the other logs, not the history.
    return logs_dir() / "jobs" / f"{job_id}.log"


# ── Handles ──────────────────────────────────────────────────────────────────


def _document(path: Path, name: str) -> Document:
    """A ``{"jobs": {...}}`` document. A missing ``jobs`` key is filled in;
    a ``jobs`` that is not an object is refused like any other corrupt file,
    so a hand edit that broke the shape is never overwritten."""

    def normalize(doc: dict) -> dict:
        jobs = doc.get("jobs")
        if jobs is None:
            doc["jobs"] = {}
        elif not isinstance(jobs, dict):
            raise CorruptDocument(name, path, "'jobs' is not an object")
        return doc

    return Document(path, schema=SCHEMA, empty=lambda: {"jobs": {}}, normalize=normalize,
                    name=name, private=True)


def jobs_document() -> Document:
    return _document(jobs_file(), "jobs.json")


def state_document() -> Document:
    return _document(state_file(), "state.json")


def runs_log(job_id: str) -> EventLog:
    """The job's run history as an :class:`EventLog`."""
    return EventLog(runs_file(job_id), rotate_bytes=RUNS_ROTATE_BYTES, keep=RUNS_KEEP)
