"""
OpenTelemetry GenAI Semantic Conventions & OTLP Exporter implementation.

Translates xo-space session messages and agent telemetry across all local runtimes
(Claude Code, Codex, OpenClaw, Hermes, Antigravity, Cursor) into OpenTelemetry
GenAI Semantic Conventions (v1.42.0+ / dedicated open-telemetry/semantic-conventions-genai repository):

Semantic Attributes & Events:
- gen_ai.provider.name: agent system / provider name (e.g. "claude_code", "codex", "openclaw")
- gen_ai.system: (legacy fallback maintained for backward compatibility)
- gen_ai.operation.name: "chat", "tool_call", "agent_run", "session"
- gen_ai.request.model: requested model (e.g. "claude-3-7-sonnet")
- gen_ai.response.model: actual model handling request
- gen_ai.usage.input_tokens / prompt_tokens: input token count
- gen_ai.usage.output_tokens / completion_tokens: output token count
- gen_ai.usage.cost: estimated session/turn cost in USD
- gen_ai.tool.name: name of tool invoked (e.g. "Bash", "View", "Edit")
- gen_ai.tool.call_id: identifier for the tool call
- gen_ai.session.id: session identifier
- Span Events: gen_ai.content.prompt, gen_ai.content.completion, gen_ai.tool.call

Provides OTLP trace JSON formatting, batch export capability, and asynchronous
background streaming to remote or local OTLP endpoints (Datadog, Jaeger, Langfuse, Arize Phoenix).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# OTel GenAI Attribute Constants (v1.42.0+ update: gen_ai.provider.name replaces gen_ai.system)
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_SYSTEM = "gen_ai.system"  # Kept for backward compatibility with legacy collectors
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_USAGE_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
GEN_AI_USAGE_COST = "gen_ai.usage.cost"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call_id"
GEN_AI_SESSION_ID = "gen_ai.session.id"
GEN_AI_PROMPT = "gen_ai.prompt"
GEN_AI_COMPLETION = "gen_ai.completion"


def _generate_id(hex_bytes: int) -> str:
    return uuid.uuid4().hex[: hex_bytes * 2]


def build_otel_genai_spans(
    session_id: str,
    agent_name: str,
    messages: List[Dict[str, Any]],
    *,
    directory: str = "",
    title: str = "",
) -> List[Dict[str, Any]]:
    """Convert a session's messages into an array of OTel Trace Spans conforming to GenAI conventions.

    Supports full trace trees (session parent span -> chat turn spans -> tool call child spans)
    with GenAI Span Events for prompts, completions, and tool payload parameters.
    """
    trace_id = _generate_id(16)
    session_span_id = _generate_id(8)
    spans: List[Dict[str, Any]] = []

    now_ns = int(time.time() * 1e9)

    provider_val = agent_name or "unknown"

    # Root session span
    session_attributes = [
        {"key": GEN_AI_PROVIDER_NAME, "value": {"stringValue": provider_val}},
        {"key": GEN_AI_SYSTEM, "value": {"stringValue": provider_val}},
        {"key": GEN_AI_SESSION_ID, "value": {"stringValue": session_id}},
        {"key": GEN_AI_OPERATION_NAME, "value": {"stringValue": "agent_session"}},
        {"key": "service.name", "value": {"stringValue": "xo-space"}},
    ]
    if directory:
        session_attributes.append({"key": "workspace.directory", "value": {"stringValue": directory}})
    if title:
        session_attributes.append({"key": "session.title", "value": {"stringValue": title}})

    session_span = {
        "traceId": trace_id,
        "spanId": session_span_id,
        "parentSpanId": "",
        "name": f"{agent_name}: {title or session_id[:8]}",
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": str(now_ns),
        "endTimeUnixNano": str(now_ns + 1000000),
        "attributes": session_attributes,
        "events": [],
        "status": {"code": 1},  # STATUS_CODE_OK
    }
    spans.append(session_span)

    # Turn & Tool call spans
    for idx, msg in enumerate(messages):
        msg_id = msg.get("id", _generate_id(8))
        role = msg.get("data", {}).get("role", "unknown")
        turn_span_id = _generate_id(8)
        model_id = msg.get("data", {}).get("model_id") or msg.get("data", {}).get("model") or "unknown"
        
        # Token metrics
        tokens = msg.get("data", {}).get("tokens") or {}
        input_tokens = tokens.get("input", 0) if isinstance(tokens, dict) else 0
        output_tokens = tokens.get("output", 0) if isinstance(tokens, dict) else 0
        total_tokens = input_tokens + output_tokens
        cost = msg.get("data", {}).get("cost", 0.0) or 0.0

        turn_attributes = [
            {"key": GEN_AI_PROVIDER_NAME, "value": {"stringValue": provider_val}},
            {"key": GEN_AI_SYSTEM, "value": {"stringValue": provider_val}},
            {"key": GEN_AI_SESSION_ID, "value": {"stringValue": session_id}},
            {"key": GEN_AI_OPERATION_NAME, "value": {"stringValue": "chat"}},
            {"key": GEN_AI_REQUEST_MODEL, "value": {"stringValue": str(model_id)}},
            {"key": GEN_AI_RESPONSE_MODEL, "value": {"stringValue": str(model_id)}},
            {"key": GEN_AI_USAGE_INPUT_TOKENS, "value": {"intValue": str(input_tokens)}},
            {"key": GEN_AI_USAGE_OUTPUT_TOKENS, "value": {"intValue": str(output_tokens)}},
            {"key": GEN_AI_USAGE_TOTAL_TOKENS, "value": {"intValue": str(total_tokens)}},
            {"key": GEN_AI_USAGE_COST, "value": {"doubleValue": float(cost)}},
        ]

        # Extract prompt / completion text from parts
        parts = msg.get("parts", [])
        prompt_text = ""
        completion_text = ""
        tool_calls: List[Dict[str, Any]] = []

        for part in parts:
            p_type = part.get("type")
            if p_type == "text":
                text_content = part.get("text", "")
                if role == "user":
                    prompt_text += text_content
                elif role == "assistant":
                    completion_text += text_content
            elif p_type == "tool_call":
                tool_calls.append(part)

        turn_events = []
        if prompt_text:
            turn_attributes.append({"key": GEN_AI_PROMPT, "value": {"stringValue": prompt_text[:2048]}})
            turn_events.append({
                "timeUnixNano": str(now_ns + (idx * 1000000)),
                "name": "gen_ai.content.prompt",
                "attributes": [{"key": "gen_ai.prompt.role", "value": {"stringValue": "user"}}, {"key": "gen_ai.prompt.text", "value": {"stringValue": prompt_text}}],
            })
        if completion_text:
            turn_attributes.append({"key": GEN_AI_COMPLETION, "value": {"stringValue": completion_text[:2048]}})
            turn_events.append({
                "timeUnixNano": str(now_ns + (idx * 1000000) + 500000),
                "name": "gen_ai.content.completion",
                "attributes": [{"key": "gen_ai.completion.role", "value": {"stringValue": "assistant"}}, {"key": "gen_ai.completion.text", "value": {"stringValue": completion_text}}],
            })

        turn_span = {
            "traceId": trace_id,
            "spanId": turn_span_id,
            "parentSpanId": session_span_id,
            "name": f"gen_ai.chat {role}",
            "kind": 3,  # SPAN_KIND_CLIENT
            "startTimeUnixNano": str(now_ns + (idx * 1000000)),
            "endTimeUnixNano": str(now_ns + ((idx + 1) * 1000000)),
            "attributes": turn_attributes,
            "events": turn_events,
            "status": {"code": 1},
        }
        spans.append(turn_span)

        # Tool Call Spans
        for tool_idx, tool in enumerate(tool_calls):
            tool_name = tool.get("name") or tool.get("tool_name") or "unknown_tool"
            call_id = tool.get("call_id") or tool.get("id") or _generate_id(4)
            tool_input = tool.get("input") or tool.get("args") or {}
            tool_span_id = _generate_id(8)

            tool_attributes = [
                {"key": GEN_AI_PROVIDER_NAME, "value": {"stringValue": provider_val}},
                {"key": GEN_AI_SYSTEM, "value": {"stringValue": provider_val}},
                {"key": GEN_AI_SESSION_ID, "value": {"stringValue": session_id}},
                {"key": GEN_AI_OPERATION_NAME, "value": {"stringValue": "tool_call"}},
                {"key": GEN_AI_TOOL_NAME, "value": {"stringValue": str(tool_name)}},
                {"key": GEN_AI_TOOL_CALL_ID, "value": {"stringValue": str(call_id)}},
            ]

            tool_events = []
            if tool_input:
                tool_events.append({
                    "timeUnixNano": str(now_ns + (idx * 1000000) + (tool_idx * 10000)),
                    "name": "gen_ai.tool.call",
                    "attributes": [
                        {"key": "gen_ai.tool.name", "value": {"stringValue": str(tool_name)}},
                        {"key": "gen_ai.tool.parameters", "value": {"stringValue": json.dumps(tool_input)}},
                    ],
                })

            tool_span = {
                "traceId": trace_id,
                "spanId": tool_span_id,
                "parentSpanId": turn_span_id,
                "name": f"gen_ai.tool {tool_name}",
                "kind": 1,  # INTERNAL
                "startTimeUnixNano": str(now_ns + (idx * 1000000) + (tool_idx * 10000)),
                "endTimeUnixNano": str(now_ns + (idx * 1000000) + ((tool_idx + 1) * 10000)),
                "attributes": tool_attributes,
                "events": tool_events,
                "status": {"code": 1},
            }
            spans.append(tool_span)

    return spans


def format_otlp_resource_spans(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Package spans into standard OTLP HTTP JSON payload format."""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "xo-space"}},
                        {"key": "service.namespace", "value": {"stringValue": "local-agent-broker"}},
                        {"key": "telemetry.sdk.language", "value": {"stringValue": "python"}},
                        {"key": "telemetry.sdk.name", "value": {"stringValue": "xo-space-opentelemetry"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "xo.space.opentelemetry.genai",
                            "version": "1.0.0",
                        },
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def export_otlp_traces(
    spans: List[Dict[str, Any]],
    endpoint: str = "",
    headers: Optional[Dict[str, str]] = None,
) -> bool:
    """Send OTLP spans to a configured collector HTTP endpoint (e.g. Datadog, Jaeger, Langfuse, Arize)."""
    target_endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not target_endpoint:
        return False

    # Default OTLP HTTP trace path if base URL provided
    if not target_endpoint.endswith("/v1/traces"):
        target_endpoint = target_endpoint.rstrip("/") + "/v1/traces"

    payload = format_otlp_resource_spans(spans)
    data = json.dumps(payload).encode("utf-8")

    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    env_headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    if env_headers:
        for kv in env_headers.split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                req_headers[k.strip()] = v.strip()

    req = urllib.request.Request(target_endpoint, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        logger.warning(f"Failed to export OTLP traces to {target_endpoint}: {exc}")
        return False


async def async_export_otlp_traces(
    spans: List[Dict[str, Any]],
    endpoint: str = "",
    headers: Optional[Dict[str, str]] = None,
) -> bool:
    """Non-blocking async wrapper to export OTLP traces without blocking main FastAPI event loop."""
    return await asyncio.to_thread(export_otlp_traces, spans, endpoint, headers)
