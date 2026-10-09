"""What failed and when, recorded the moment it happens and kept.

The Space-level record xo-doctor reads back (``services/doctor/crashes.py``):
every component writes here, so it is a top-level package (the placement
rule in CLAUDE.md), not part of the agent tree and not part of the doctor,
whose checks only read.

- :mod:`services.health.recorder`: ``record()`` one failure; coalesced,
  capped, private, and never raises into its caller.
- :mod:`services.health.session`: this run's marker, so a run that ended
  without shutting down (kill -9, out of memory, power loss) is noticed at
  the next start; ``faulthandler`` for a hard crash's traceback.
- :mod:`services.health.annotations`: what each run was (version, platform,
  switches), kept per boot.

Files live in ``~/.quirq/setup/health/`` (``services/storage/layout.py``).
"""
