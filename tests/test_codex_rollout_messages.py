"""Codex rollout → MessageResponse conversion.

Pins the user-turn side of ``codex/sessions._convert``. Newer codex builds
stopped writing the legacy ``event_msg``/``user_message`` event — a user turn
is only recorded as a ``response_item`` ``message`` with ``role: "user"`` — so
the converter has to read both shapes without double-rendering the turn on the
builds that still emit each one.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from services.cowork_agent.adapters.codex import sessions as cs

SID = "0f5b2c41-3d6a-4f52-9c88-1a7e04b6d931"

ENV_BLOCK = (
    "<environment_context>\n  <cwd>/home/agent/xo-projects/demo</cwd>\n"
    "  <sandbox_mode>workspace-write</sandbox_mode>\n</environment_context>"
)


def line(kind: str, payload: dict, ts: str = "2026-09-20T10:00:00.000Z") -> dict:
    return {"timestamp": ts, "type": kind, "payload": payload}


def user_item(text: str) -> dict:
    """A user turn as newer codex records it: response_item + input_text."""
    return line("response_item", {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}],
    })


def user_event(text: str) -> dict:
    """A user turn as older codex ALSO recorded it: the legacy event."""
    return line("event_msg", {"type": "user_message", "message": text})


def assistant_item(text: str) -> dict:
    return line("response_item", {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    })


def convert(rows: list[dict]) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rollout.jsonl"
        path.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        return cs._convert(SID, path)


def roles(messages: list[dict]) -> list[str]:
    return [m["data"]["role"] for m in messages]


def text_of(message: dict) -> str:
    return "".join(
        p["data"].get("text", "") for p in message["parts"]
        if p["data"].get("type") == "text"
    )


class CodexUserTurnTests(unittest.TestCase):
    def test_user_turns_render_without_the_legacy_event(self) -> None:
        """The reported bug: assistant text survives, user bubbles vanish."""
        out = convert([
            line("session_meta", {"id": SID}),
            user_item(ENV_BLOCK),
            user_item("hello"),
            assistant_item("Hello! What would you like to build?"),
            user_item("typescript"),
            assistant_item("Great — TypeScript it is."),
        ])

        self.assertEqual(roles(out), ["user", "assistant", "user", "assistant"])
        self.assertEqual(text_of(out[0]), "hello")
        self.assertEqual(text_of(out[2]), "typescript")

    def test_injected_context_blocks_never_become_a_bubble(self) -> None:
        out = convert([
            user_item(ENV_BLOCK),
            user_item("<user_instructions>\nbe terse\n</user_instructions>"),
            assistant_item("ok"),
        ])
        self.assertEqual(roles(out), ["assistant"])

    def test_injected_block_prefixing_a_real_prompt_is_stripped(self) -> None:
        out = convert([
            user_item(f"{ENV_BLOCK}\n\nship the login fix"),
            assistant_item("on it"),
        ])
        self.assertEqual(roles(out), ["user", "assistant"])
        self.assertEqual(text_of(out[0]), "ship the login fix")

    def test_a_turn_recorded_twice_renders_once(self) -> None:
        """Builds that emit BOTH shapes must not double-render the turn."""
        out = convert([
            user_item("hello"),
            user_event("hello"),
            assistant_item("hi"),
        ])
        self.assertEqual(roles(out), ["user", "assistant"])

    def test_legacy_only_rollouts_still_convert(self) -> None:
        out = convert([
            user_event("hello"),
            assistant_item("hi"),
        ])
        self.assertEqual(roles(out), ["user", "assistant"])
        self.assertEqual(text_of(out[0]), "hello")

    def test_a_user_turn_does_not_steal_the_assistant_timestamp(self) -> None:
        out = convert([
            line("response_item", {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            }, ts="2026-09-20T10:00:00.000Z"),
            line("response_item", {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}],
            }, ts="2026-09-20T10:00:09.000Z"),
        ])
        self.assertEqual(out[0]["time_created"], "2026-09-20T10:00:00.000Z")
        self.assertEqual(out[1]["time_created"], "2026-09-20T10:00:09.000Z")

    def test_tool_chips_stay_attached_to_their_turn(self) -> None:
        out = convert([
            user_item("run it"),
            line("response_item", {
                "type": "custom_tool_call", "call_id": "c1",
                "name": "shell", "input": "ls -la",
            }),
            line("response_item", {
                "type": "custom_tool_call_output", "call_id": "c1", "output": "README.md",
            }),
            assistant_item("done"),
        ])
        self.assertEqual(roles(out), ["user", "assistant"])
        kinds = [p["data"]["type"] for p in out[1]["parts"]]
        self.assertEqual(kinds, ["tool", "text"])
        self.assertEqual(out[1]["parts"][0]["data"]["state"]["output"], "README.md")


class CodexSessionTitleTests(unittest.TestCase):
    """The sidebar title comes from the same prompt source as the bubbles."""

    def enrich(self, rows: list[dict]):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text(
                "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
            )
            with unittest.mock.patch.object(
                cs._paths, "find_rollout", return_value=path
            ):
                return cs.enrich_project_session({"nativeSessionId": SID}, "k", "agent")

    def test_title_uses_the_first_typed_prompt_without_the_legacy_event(self) -> None:
        _created, title, _agent = self.enrich([
            line("session_meta", {"timestamp": "2026-09-20T10:00:00.000Z"}),
            user_item(ENV_BLOCK),
            user_item("add a login page"),
            assistant_item("on it"),
        ])
        self.assertEqual(title, "add a login page")

    def test_session_meta_still_supplies_time_created(self) -> None:
        created, _title, _agent = self.enrich([
            line("session_meta", {"timestamp": "2026-09-20T10:00:00.000Z"}),
            user_item("hello"),
        ])
        self.assertEqual(created, "2026-09-20T10:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
