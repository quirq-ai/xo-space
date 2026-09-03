"""
OpenTelemetry GenAI Exporter & OTLP endpoints.

Provides endpoints to retrieve OTel GenAI compliant spans and export trace data
for local agent sessions to any OTLP-compliant collector (Datadog, Jaeger,
Langfuse, Arize Phoenix, Grafana Tempo).
"""

from typing import Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from services.cowork_agent.engine.sessions_io import (
    find_session_backend,
    load_all_sessions,
)
from services.cowork_agent.adapters.loader import try_load_capability
from services.cowork_agent.opentelemetry_exporter import (
    build_otel_genai_spans,
    export_otlp_traces,
    format_otlp_resource_spans,
)

router = APIRouter()


class OTLPExportRequest(BaseModel):
    endpoint: Optional[str] = None
    headers: Optional[Dict[str, str]] = None


@router.get("/api/sessions/{session_id}/otel")
def get_session_otel_trace(session_id: str):
    """Retrieve session trace spans formatted per OpenTelemetry GenAI Semantic Conventions."""
    backend = find_session_backend(session_id)
    if not backend:
        raise HTTPException(status_code=404, detail="Session not found")

    mod = try_load_capability("sessions", agent=backend)
    fn = getattr(mod, "get_messages", None) if mod else None
    messages = fn(session_id) if fn else []

    all_sessions = load_all_sessions()
    session_meta = next((s for s in all_sessions if s["id"] == session_id), {})

    spans = build_otel_genai_spans(
        session_id=session_id,
        agent_name=backend,
        messages=messages,
        directory=session_meta.get("directory", ""),
        title=session_meta.get("title", ""),
    )

    return format_otlp_resource_spans(spans)


@router.post("/api/sessions/{session_id}/otel/export")
def export_session_otel_trace(session_id: str, req: OTLPExportRequest):
    """Export a session's OTel trace to a target OTLP collector endpoint."""
    backend = find_session_backend(session_id)
    if not backend:
        raise HTTPException(status_code=404, detail="Session not found")

    mod = try_load_capability("sessions", agent=backend)
    fn = getattr(mod, "get_messages", None) if mod else None
    messages = fn(session_id) if fn else []

    all_sessions = load_all_sessions()
    session_meta = next((s for s in all_sessions if s["id"] == session_id), {})

    spans = build_otel_genai_spans(
        session_id=session_id,
        agent_name=backend,
        messages=messages,
        directory=session_meta.get("directory", ""),
        title=session_meta.get("title", ""),
    )

    success = export_otlp_traces(spans, endpoint=req.endpoint or "", headers=req.headers)
    if not success:
        raise HTTPException(status_code=502, detail="Failed to export OTLP traces to target endpoint")

    return {"ok": True, "exported_spans": len(spans)}
