"""What the jobs module emits and raises."""

#: Event lines this module writes: every line of ``jobs/runs/<id>.jsonl``
#: is a ``job.run`` whose ``status`` is the outcome (ok, failed, timed_out,
#: missing_binary, error, skipped, lost).
TYPES = ("job.run",)

#: No signals yet.
SIGNALS = ()
