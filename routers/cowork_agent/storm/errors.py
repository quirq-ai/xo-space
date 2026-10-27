"""``http_error`` for storm's typed failures, keeping the extra field a
client acts on: the feature-list key of a ``501 unsupported`` (ch 17 §1),
the count a ``409 would_stop_runs`` must be confirmed with, the sentence
a rule must be made with (``400 rule_sentence_mismatch``, ch 15 §3), what
removing a worktree would lose with its confirmation (``409 would_lose_work``,
ch 13 §4.3), and the stored summary of a run whose transcript is gone
(``410 transcript_gone``, ch 14 §3)."""

from __future__ import annotations

from fastapi import HTTPException

from routers.cowork_agent.bff.errors import http_error
from services.errors import ServiceError


def storm_error(exc: ServiceError) -> HTTPException:
    error = http_error(exc)
    for extra in ("feature", "count", "sentence", "what", "confirm", "summary"):
        if hasattr(exc, extra):
            error.detail[extra] = getattr(exc, extra)
    return error
