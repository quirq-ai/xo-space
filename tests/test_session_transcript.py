from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.sessions import router
from services.cowork_agent import session_transcript as st

SID = "f2d46667-4ac1-4e03-973e-c9f86832250d"


def part(kind: str, **data) -> dict:
    return {"id": "p", "message_id": "m", "session_id": SID, "time_created": "t", "data": {"type": kind, **data}}


def msg(mid: str, role: str, *parts: dict) -> dict:
    return {"id": mid, "session_id": SID, "time_created": "t", "data": {"role": role}, "parts": list(parts)}


# The shape GET /api/messages returned for a real Claude Code turn that ran two tools.
RECORD = [
    msg("u1", "user", part("text", text="Summarize what this project is")),
    msg("a1", "assistant", part("text", text="I'll look at the project files."),
        part("tool", tool="Bash", call_id="c1", state={"status": "completed",
             "input": {"command": "ls -la && cat README.md", "description": "List files"}, "output": "..."})),
    msg("a2", "assistant", part("tool", tool="Bash", call_id="c2", state={"status": "completed",
             "input": {"command": "cat package.json\nfind . -type f"}, "output": "..."})),
    msg("a3", "assistant", part("reasoning", text="private thoughts"),
        part("text", text="**read-in-terminal** — a Next.js template.")),
]


class BuildTranscriptTests(unittest.TestCase):
    def test_one_bubble_per_turn_text_only(self) -> None:
        out = st.build_transcript("Summarize what this project is", RECORD)
        self.assertEqual(out["title"], "Summarize what this project is")
        self.assertEqual(out["messages"], [
            {"id": "u1", "role": "user", "content": "Summarize what this project is"},
            {"id": "a1", "role": "assistant",
             "content": "I'll look at the project files.\n\n**read-in-terminal** — a Next.js template."},
        ])

    def test_tools_opt_in_as_one_line_each(self) -> None:
        out = st.build_transcript("t", RECORD, include_tools=True)
        self.assertEqual(out["messages"][1]["content"],
                         "I'll look at the project files.\n\n[Bash] ls -la && cat README.md\n\n"
                         "[Bash] cat package.json\n\n**read-in-terminal** — a Next.js template.")

    def test_reasoning_never_appears_and_empty_bubbles_are_dropped(self) -> None:
        only_tools = [msg("u1", "user", part("text", text="go")),
                      msg("a1", "assistant", part("tool", tool="Read", state={"input": {"path": "x"}})),
                      msg("a2", "assistant", part("reasoning", text="hmm"))]
        out = st.build_transcript("t", only_tools)
        self.assertEqual(out["messages"], [{"id": "u1", "role": "user", "content": "go"}])
        self.assertNotIn("hmm", str(st.build_transcript("t", RECORD, include_tools=True)))

    def test_user_turns_are_not_merged_and_unknown_roles_are_skipped(self) -> None:
        record = [msg("u1", "user", part("text", text="a")), msg("u2", "user", part("text", text="b")),
                  msg("s1", "system", part("text", text="ignored")), msg("a1", "assistant", part("text", text="c"))]
        out = st.build_transcript("t", record)
        self.assertEqual([m["id"] for m in out["messages"]], ["u1", "u2", "a1"])

    def test_title_is_cut_on_a_word_boundary_at_fifty(self) -> None:
        long = "Please write a very long and detailed explanation of the Euler equations for me"
        cut = st.truncate_title(long)
        self.assertLessEqual(len(cut), 50)
        self.assertTrue(cut.endswith("…"))
        self.assertNotIn("  ", cut)
        self.assertEqual(st.truncate_title("  short   title "), "short title")
        self.assertEqual(st.truncate_title("x" * 50), "x" * 50)


class TranscriptRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_returns_the_projection_and_passes_the_tools_flag(self) -> None:
        with patch.object(st, "load_all_sessions", return_value=[{"id": SID, "title": "Summarize what this project is"}]), \
             patch.object(st, "find_session_backend", return_value="x"), \
             patch.object(st, "try_load_capability", return_value=type("M", (), {"get_messages": staticmethod(lambda sid: RECORD)})):
            plain = self.client.get(f"/api/sessions/{SID}/transcript").json()
            with_tools = self.client.get(f"/api/sessions/{SID}/transcript?tools=true").json()
        self.assertEqual([m["role"] for m in plain["messages"]], ["user", "assistant"])
        self.assertNotIn("[Bash]", plain["messages"][1]["content"])
        self.assertIn("[Bash] ls -la", with_tools["messages"][1]["content"])

    def test_unknown_session_is_404(self) -> None:
        with patch.object(st, "load_all_sessions", return_value=[]):
            r = self.client.get("/api/sessions/nope/transcript")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"detail": "Session not found"})
