import unittest
from unittest.mock import patch, MagicMock

from services.cowork_agent.opentelemetry_exporter import (
    build_otel_genai_spans,
    format_otlp_resource_spans,
    GEN_AI_SYSTEM,
    GEN_AI_REQUEST_MODEL,
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_TOOL_NAME,
)

class TestOpenTelemetryGenAIExporter(unittest.TestCase):
    def test_build_otel_genai_spans_basic(self):
        session_id = "test-session-123"
        agent_name = "claude_code"
        messages = [
            {
                "id": "msg-1",
                "data": {
                    "role": "user",
                    "model_id": "claude-3-7-sonnet",
                    "tokens": {"input": 100, "output": 0},
                    "cost": 0.001,
                },
                "parts": [{"type": "text", "text": "Refactor auth logic"}],
            },
            {
                "id": "msg-2",
                "data": {
                    "role": "assistant",
                    "model_id": "claude-3-7-sonnet",
                    "tokens": {"input": 50, "output": 200},
                    "cost": 0.005,
                },
                "parts": [
                    {"type": "text", "text": "Refactoring now..."},
                    {"type": "tool_call", "name": "Edit", "call_id": "tool-1"},
                ],
            },
        ]

        spans = build_otel_genai_spans(
            session_id=session_id,
            agent_name=agent_name,
            messages=messages,
            directory="/home/user/xo-projects/test",
            title="Test Session",
        )

        self.assertGreaterEqual(len(spans), 4)  # 1 session span + 2 turn spans + 1 tool span

        # Check session root span
        root_span = spans[0]
        self.assertEqual(root_span["name"], "claude_code: Test Session")
        attr_dict = {a["key"]: a["value"] for a in root_span["attributes"]}
        self.assertEqual(attr_dict[GEN_AI_SYSTEM]["stringValue"], "claude_code")

        # Check tool span
        tool_spans = [s for s in spans if s["name"].startswith("gen_ai.tool")]
        self.assertEqual(len(tool_spans), 1)
        tool_attr = {a["key"]: a["value"] for a in tool_spans[0]["attributes"]}
        self.assertEqual(tool_attr[GEN_AI_TOOL_NAME]["stringValue"], "Edit")

    def test_format_otlp_resource_spans(self):
        spans = [{"spanId": "123"}]
        payload = format_otlp_resource_spans(spans)
        self.assertIn("resourceSpans", payload)
        self.assertEqual(
            payload["resourceSpans"][0]["scopeSpans"][0]["spans"], spans
        )

if __name__ == "__main__":
    unittest.main()
