"""xo-doctor: tells a person when Quirq's on-disk state stopped being consistent.

Design: docs/xo-doctor/ARCHITECTURE.md. A check run only reads. The one action,
``leftovers.move_aside``, moves a leftover runtime folder into ``quarantine/``
when a person confirms it. This package names no agent and imports no FastAPI.
"""
