"""``audr.jsonl`` sink — one OpenAudr usage record per metered operation.

AUDR (Agent Usage Detail Record, https://openaudr.dev/spec/v1.0.0/) is the
open format for agent usage: one JSON record per model call or tool call,
joinable on ``(run.run_id, run.span_id)`` and deduplicated by ``record_id``.
xo-space observes the runs, so it emits as the ``harness`` component.

Mapping, from the normalised events every adapter already produces:

* :class:`UsageObserved` → a ``model`` / ``generation`` record with the
  turn's token counters under ``usage.llm``.
* :class:`ToolUseObserved` → a ``tool`` / ``tool_execution`` record, one
  ``invocation`` under ``usage.tool``. The tool **name** only: inputs never
  reach an event, so they never reach a record.

Every other event type is ignored. The sink always runs; attribution is the
fixed minimum the schema requires (``environment: development``), with no
account or labels.

Records are built and validated with the official ``audr`` SDK, then appended
here rather than through ``audr.Client`` (which batches on an event loop the
synchronous watcher tick does not have). A record the SDK rejects is skipped
and logged by issue code and JSON pointer only — never by value.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import audr

from services.cowork_agent.visualizer.atomic_write import append_jsonl, rotate_jsonl
from services.cowork_agent.visualizer.ingest.events import (
    Event,
    ToolUseObserved,
    UsageObserved,
)

logger = logging.getLogger(__name__)


_AUDR_FILE = Path("audr.jsonl")
_ROTATE_BYTES = 8 * 1024 * 1024  # 8 MB, the same policy as timeline.jsonl
_MAX_ROTATIONS_KEEP = 5

#: Mirrors the FastAPI app version in ``server.py``.
_EMITTER = audr.Emitter(component="harness", name="xo-space", version="1.0.0")
_ATTRIBUTION = audr.Attribution(environment="development")

#: Model-name prefix → AUDR provider slug. Keyed on the model, never the
#: runtime, so no agent is named here; an unknown model falls back to the
#: runtime string, slugged.
_PROVIDER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("codex", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini", "google"),
    ("grok", "xai"),
)

#: The spec's slug for a tool the harness runs locally.
_LOCAL_TOOL_PROVIDER = "self-hosted"


def _slug(value: str) -> str:
    """``^[a-z0-9-]+$``: lowercase, every other character becomes ``-``."""
    out = "".join(c if c.isascii() and c.isalnum() else "-" for c in value.lower()).strip("-")
    return out or "unknown"


def _provider_for(model: Optional[str], runtime: str) -> str:
    name = (model or "").lower()
    for prefix, provider in _PROVIDER_PREFIXES:
        if name.startswith(prefix):
            return provider
    return _slug(runtime)


def _event_time(ts: str) -> Optional[datetime]:
    """``ts`` as an aware UTC datetime at millisecond precision, or ``None``."""
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.replace(microsecond=moment.microsecond - moment.microsecond % 1000)


def _span_id(kind: str, ev: Event, name: str, ordinal: int) -> str:
    """Stable per operation and unique within the run (the session)."""
    key = f"{ev.native_session_id}|{ev.ts}|{kind}|{name}|{ordinal}"
    return f"{kind}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def _run(ev: Event, span_id: str) -> audr.Run:
    return audr.Run(run_id=ev.native_session_id, span_id=span_id, run_type="agent_run")


def _model_record(ev: UsageObserved, when: datetime, ordinal: int) -> audr.AUDR:
    name = ev.model or "unknown"
    return audr.AUDR(
        emitter=_EMITTER,
        timing=audr.Timing(event_time=when, duration_ms=ev.latency_ms),
        resource=audr.Resource(
            provider=_provider_for(ev.model, ev.runtime),
            type="model",
            name=name,
            operation="generation",
            modality="text",
        ),
        run=_run(ev, _span_id("model", ev, name, ordinal)),
        attribution=_ATTRIBUTION,
        usage=audr.Usage(
            llm=audr.LlmUsage(
                input_tokens=ev.input_tokens,
                output_tokens=ev.output_tokens,
                cache_read_tokens=ev.cache_read_input_tokens,
                cache_write_tokens=ev.cache_creation_input_tokens,
                requests=1,
            )
        ),
    )


def _tool_record(ev: ToolUseObserved, when: datetime, ordinal: int) -> audr.AUDR:
    return audr.AUDR(
        emitter=_EMITTER,
        timing=audr.Timing(event_time=when),
        resource=audr.Resource(
            provider=_LOCAL_TOOL_PROVIDER,
            type="tool",
            name=ev.tool,
            operation="tool_execution",
        ),
        run=_run(ev, _span_id("tool", ev, ev.tool, ordinal)),
        attribution=_ATTRIBUTION,
        usage=audr.Usage(tool=audr.ToolUsage(type="invocation", call_count=1)),
    )


def _record_for(ev: Event, ordinal: int) -> Optional[dict]:
    """The validated record for ``ev`` as plain JSON, or ``None`` to skip it."""
    if not isinstance(ev, (UsageObserved, ToolUseObserved)):
        return None
    if isinstance(ev, ToolUseObserved) and not ev.tool:
        return None
    when = _event_time(ev.ts)
    if when is None:
        logger.warning("audr: skipped an event with an unparseable timestamp")
        return None
    try:
        if isinstance(ev, UsageObserved):
            record = _model_record(ev, when, ordinal)
        else:
            record = _tool_record(ev, when, ordinal)
        record.ensure_valid()
    except audr.ValidationError as exc:
        _log_rejected(exc.issues)
        return None
    except ValueError as exc:  # pydantic: a field outside its declared shape
        logger.warning("audr: skipped a record the model refused (%s)", type(exc).__name__)
        return None
    return record.to_dict()


def _log_rejected(issues: Iterable[audr.ValidationIssue]) -> None:
    # ``str(issue)`` is ``"<code> at <pointer>"``: the SDK never puts a value in it.
    detail = "; ".join(str(issue) for issue in issues)
    logger.warning("audr: skipped a non-conformant record (%s)", detail)


def apply(root: Path, events: Iterable[Event]) -> list[dict]:
    """Append one AUDR record per metered event; returns the records written."""
    rotate_jsonl(root / _AUDR_FILE, max_bytes=_ROTATE_BYTES, keep=_MAX_ROTATIONS_KEEP)

    records: list[dict] = []
    for ordinal, ev in enumerate(events):
        record = _record_for(ev, ordinal)
        if record is not None:
            records.append(record)

    if records:
        append_jsonl(root / _AUDR_FILE, records)
    return records
