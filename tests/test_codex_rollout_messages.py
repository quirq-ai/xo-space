"""Codex rollout → MessageResponse conversion.

Pins the user-turn side of ``codex/sessions._convert``. Newer codex builds
stopped writing the legacy ``event_msg``/``user_message`` event — a user turn
is only recorded as a ``response_item`` ``message`` with ``role: "user"`` — and
they send the turn's injected context (plugin recommendations, AGENTS.md,
environment context) as a SEPARATE ``role: "user"`` message. Codex labels every
block in ``internal_chat_message_metadata_passthrough.content_item_kinds``, so
the two are told apart by that label, never by inspecting the text.
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
    "  <shell>bash</shell>\n</environment_context>"
)
PLUGINS_BLOCK = (
    "<recommended_plugins>\n"
    "Here is a list of plugins that are available but not installed.\n\n"
    "- Example (app-0000@example-remote)\n"
    "</recommended_plugins>"
)
AGENTS_BLOCK = "# AGENTS.md instructions for /home/agent/xo-projects/demo\n\nboot ritual"

# The three kinds codex labelled the injected message with in a real rollout.
CONTEXT_KINDS = [
    "plugins.recommendations",
    "agents_md.instructions",
    "environments.environment_context",
]


def line(kind: str, payload: dict, ts: str = "2026-09-20T10:00:00.000Z") -> dict:
    return {"timestamp": ts, "type": kind, "payload": payload}


def labelled_user_item(pairs, ts: str = "2026-09-20T10:00:00.000Z") -> dict:
    """A user message with codex's own per-block ``content_item_kinds``."""
    return line("response_item", {
        "type": "message", "role": "user", "id": "msg_test",
        "content": [{"type": "input_text", "text": text} for _kind, text in pairs],
        "internal_chat_message_metadata_passthrough": {
            "turn_id": "turn_test",
            "content_item_kinds": [kind for kind, _text in pairs],
        },
    }, ts=ts)


def user_item(text: str, ts: str = "2026-09-20T10:00:00.000Z") -> dict:
    """The typed turn: one block, labelled ``user.text``."""
    return labelled_user_item([("user.text", text)], ts=ts)


def context_item() -> dict:
    """The injected-context message codex sends alongside the typed turn."""
    return labelled_user_item(list(zip(
        CONTEXT_KINDS, [PLUGINS_BLOCK, AGENTS_BLOCK, ENV_BLOCK],
    )))


def unlabelled_user_item(text: str) -> dict:
    """A user turn from a codex build predating ``content_item_kinds``."""
    return line("response_item", {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}],
    })


def developer_item(text: str) -> dict:
    return line("response_item", {
        "type": "message", "role": "developer",
        "content": [{"type": "input_text", "text": text}],
    })


def user_event(text: str) -> dict:
    """A user turn as older codex ALSO recorded it: the legacy event."""
    return line("event_msg", {"type": "user_message", "message": text})


def assistant_item(text: str, ts: str = "2026-09-20T10:00:00.000Z") -> dict:
    return line("response_item", {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }, ts=ts)


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
            context_item(),
            user_item("hello"),
            assistant_item("Hello! What would you like to build?"),
            user_item("typescript"),
            assistant_item("Great — TypeScript it is."),
        ])

        self.assertEqual(roles(out), ["user", "assistant", "user", "assistant"])
        self.assertEqual(text_of(out[0]), "hello")
        self.assertEqual(text_of(out[2]), "typescript")

    def test_the_injected_context_message_never_becomes_a_bubble(self) -> None:
        out = convert([context_item(), assistant_item("ok")])
        self.assertEqual(roles(out), ["assistant"])

    def test_a_context_kind_codex_adds_later_is_excluded_too(self) -> None:
        """Selection is by label, so an unknown kind needs no code change."""
        out = convert([
            labelled_user_item([
                ("something.not_invented_yet", "<whatever>\nnew context\n</whatever>"),
                ("user.text", "ship the login fix"),
            ]),
            assistant_item("on it"),
        ])
        self.assertEqual(roles(out), ["user", "assistant"])
        self.assertEqual(text_of(out[0]), "ship the login fix")

    def test_a_prompt_is_never_rewritten_whatever_it_contains(self) -> None:
        """Text that looks like injected context still renders verbatim."""
        typed = "<environment_context>\nwhy does this show up?\n</environment_context>"
        out = convert([user_item(typed), assistant_item("looking")])
        self.assertEqual(roles(out), ["user", "assistant"])
        self.assertEqual(text_of(out[0]), typed)

    def test_the_frontend_workspace_preamble_is_trimmed(self) -> None:
        out = convert([
            user_item(
                "hello there\n\n---\n\n> **Project context**\n> Working directory: `/x`"
            ),
            assistant_item("hi"),
        ])
        self.assertEqual(text_of(out[0]), "hello there")

    def test_developer_turns_stay_hidden(self) -> None:
        out = convert([
            developer_item("<skills_instructions>\n## Skills\n</skills_instructions>"),
            user_item("hello"),
            assistant_item("hi"),
        ])
        self.assertEqual(roles(out), ["user", "assistant"])

    def test_a_turn_recorded_twice_renders_once(self) -> None:
        """Builds that emit BOTH shapes must not double-render the turn."""
        out = convert([user_item("hello"), user_event("hello"), assistant_item("hi")])
        self.assertEqual(roles(out), ["user", "assistant"])

    def test_legacy_only_rollouts_still_convert(self) -> None:
        out = convert([user_event("hello"), assistant_item("hi")])
        self.assertEqual(roles(out), ["user", "assistant"])
        self.assertEqual(text_of(out[0]), "hello")

    def test_unlabelled_rollouts_fall_back_without_dropping_the_turn(self) -> None:
        out = convert([
            unlabelled_user_item(ENV_BLOCK),
            unlabelled_user_item("hello"),
            assistant_item("hi"),
        ])
        self.assertEqual(roles(out), ["user", "assistant"])
        self.assertEqual(text_of(out[0]), "hello")

    def test_mislabelled_metadata_is_not_trusted(self) -> None:
        """Kinds that do not line up with the blocks must not blank the turn."""
        row = labelled_user_item([("user.text", "hello")])
        row["payload"]["internal_chat_message_metadata_passthrough"] = {
            "content_item_kinds": ["user.text", "environments.environment_context"],
        }
        out = convert([row, assistant_item("hi")])
        self.assertEqual(roles(out), ["user", "assistant"])
        self.assertEqual(text_of(out[0]), "hello")

    def test_a_user_turn_does_not_steal_the_assistant_timestamp(self) -> None:
        out = convert([
            user_item("hello", ts="2026-09-20T10:00:00.000Z"),
            assistant_item("hi", ts="2026-09-20T10:00:09.000Z"),
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


def reasoning_item(summary: list[str] | None, encrypted: str | None = "opaque") -> dict:
    """A codex ``reasoning`` response item: the model's own text is
    ``encrypted_content`` (opaque), the readable part is ``summary``."""
    payload: dict = {"type": "reasoning", "id": "rs_test"}
    if summary is not None:
        payload["summary"] = [{"type": "summary_text", "text": t} for t in summary]
    if encrypted is not None:
        payload["encrypted_content"] = encrypted
    return line("response_item", payload)


class CodexReasoningTests(unittest.TestCase):
    """What claude_code already records (a ``reasoning`` part per turn) codex
    records too, from the readable summary; the encrypted body never does."""

    def test_a_reasoning_summary_is_recorded_as_a_reasoning_part(self) -> None:
        out = convert([
            user_item("why is the build red"),
            reasoning_item(["Checking the failing job.", "The lockfile is stale."]),
            assistant_item("The lockfile is stale; run install."),
        ])
        self.assertEqual(roles(out), ["user", "assistant"])
        kinds = [p["data"]["type"] for p in out[1]["parts"]]
        self.assertEqual(kinds, ["reasoning", "text"])
        self.assertEqual(out[1]["parts"][0]["data"]["text"],
                         "Checking the failing job.\n\nThe lockfile is stale.")
        self.assertNotIn("opaque", json.dumps(out))

    def test_encrypted_only_reasoning_records_nothing(self) -> None:
        out = convert([user_item("hi"), reasoning_item(None), assistant_item("hello")])
        self.assertEqual([p["data"]["type"] for p in out[1]["parts"]], ["text"])
        out = convert([user_item("hi"), reasoning_item([]), assistant_item("hello")])
        self.assertEqual([p["data"]["type"] for p in out[1]["parts"]], ["text"])

    def test_reasoning_stays_out_of_the_transcript(self) -> None:
        from services.cowork_agent import session_transcript

        out = convert([
            user_item("why"),
            reasoning_item(["private summary"]),
            assistant_item("because"),
        ])
        transcript = session_transcript.build_transcript("why", out)
        self.assertEqual([m["content"] for m in transcript["messages"]], ["why", "because"])


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

    def test_title_uses_the_first_typed_prompt_not_the_context_message(self) -> None:
        _created, title, _agent = self.enrich([
            line("session_meta", {"timestamp": "2026-09-20T10:00:00.000Z"}),
            context_item(),
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
