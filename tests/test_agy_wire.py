"""The agy runtime's wire and its pure parts (storm ch 04 §7–§10): agy
1.4.10's stream-json as events, its tools in neutral terms, its command line,
its account and usage report, and its transcripts.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.antigravity.runtime import account, argv as a, tools
from services.cowork_agent.adapters.antigravity.runtime import transcript as tr
from services.cowork_agent.adapters.antigravity.runtime.wire import Wire
from tests.fakes import agy

PARENT = "11111111-2222-4333-8444-555555555555"
CHILD = "66666666-7777-4888-8999-aaaaaaaaaaaa"
SKIP_FLAGS = ("--dangerously-skip-permissions",)


def argv_for(spec: c.SessionSpec) -> list[str]:
    """What one session's agy runs with (its first process)."""
    return a.argv("agy", spec, mode=spec.permissions.mode, resume=spec.resume, model=spec.model,
                  effort=spec.effort, schema_file="/s.json" if spec.output_schema is not None else None)


def _line(step: dict) -> str:
    return json.dumps(step)


def write_transcripts(home: Path) -> tuple[str, str]:
    """A conversation that started one sub-agent, as agy 1.4.10 keeps them."""
    brain = home / ".gemini" / "antigravity-cli" / "brain"
    created = json.dumps({"conversationId": CHILD, "logAbsoluteUri": f"file:///x/{CHILD}/transcript.jsonl"},
                         indent=2)
    parent = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
         "created_at": "2026-10-29T14:06:28Z", "content": "<USER_REQUEST>\nCheck the build.\n</USER_REQUEST>"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
         "created_at": "2026-10-29T14:06:28Z", "tool_calls": [
             {"name": "invoke_subagent", "args": {"Role": "Build Checker", "TypeName": "self", "Prompt": "Run make"}}]},
        {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-10-29T14:06:32Z",
         "content": f"Created At: 2026-10-29T14:06:32Z\nCompleted At: 2026-10-29T14:06:32Z\n"
                    f"Created the following subagent:\n{created}"},
        {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
         "created_at": "2026-10-29T14:06:46Z", "content": "The build passes."},
    ]
    child = [
        {"step_index": 0, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE",
         "created_at": "2026-10-29T14:06:32Z",
         "content": "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-29T14:06:32Z sender=" + PARENT +
                    " priority=MESSAGE_PRIORITY_HIGH content=Run make and report.\n</SYSTEM_MESSAGE>"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
         "created_at": "2026-10-29T14:06:32Z", "thinking": "Run the build first.\nThen report.",
         "tool_calls": [{"name": "run_command", "args": {"CommandLine": "make", "toolSummary": "Build"}},
                        {"name": "write_file", "args": {"Path": "/w/.agents/hooks.json", "Content": "{}"}}]},
        {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-10-29T14:06:36Z",
         "content": "Created At: 2026-10-29T14:06:36Z\nCompleted At: 2026-10-29T14:06:36Z\nCommand exited with "
                    "code 0.\n\nCommand output:\nok\r\n"},
        {"step_index": 3, "source": "MODEL", "type": "GENERIC", "status": "ERROR", "created_at": "2026-10-29T14:06:37Z",
         "error": '"tool call denied by pre-tool hook: protected"', "content": "Encountered error"},
        {"step_index": 4, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
         "created_at": "2026-10-29T14:06:42Z", "tool_calls": [{"name": "send_message", "args": {"Message": "ok"}}]},
        {"step_index": 5, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-10-29T14:06:46Z",
         "content": f"Message sent to \"{PARENT}\"."},
        {"step_index": 6, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
         "created_at": "2026-10-29T14:06:46Z", "content": "Results reported back to parent agent."},
    ]
    for conv, steps in ((PARENT, parent), (CHILD, child)):
        logs = brain / conv / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "transcript_full.jsonl").write_text("\n".join(_line(s) for s in steps) + "\n", encoding="utf-8")
    return PARENT, CHILD


#: Real lines of agy 1.4.10 (live, 2026-10-29), cut to what the wire reads.
LIVE_TURN = [
    {"event": "step_update", "step_update": {"conversation_id": agy.CONV, "step_index": 0, "state": "DONE",
                                             "step_type": "user_input"}},
    {"event": "step_update", "step_update": {"conversation_id": agy.CONV, "step_index": 1, "state": "DONE",
                                             "step_type": "agent_response", "duration_seconds": 3.2,
                                             "usage": {"input_tokens": 7608, "output_tokens": 37,
                                                       "thinking_tokens": 0, "cache_read_tokens": 0}}},
    {"event": "step_update", "step_update": {"conversation_id": agy.CONV, "step_index": 2, "state": "ACTIVE",
                                             "step_type": "tool", "tool_name": "run_command",
                                             "tool_info": {"name": "run_command",
                                                           "parameters": {"CommandLine": "echo A1"}}}},
    {"event": "step_update", "step_update": {"conversation_id": agy.CONV, "step_index": 2, "state": "DONE",
                                             "step_type": "tool", "tool_name": "run_command",
                                             "tool_info": {"name": "run_command", "parameters": {"CommandLine": "echo A1"},
                                                           "output": "Command exited with code 0.\n\nCommand output:\nA1\r\n"}}},
    {"event": "step_update", "step_update": {"conversation_id": agy.CONV, "step_index": 4, "state": "ACTIVE",
                                             "step_type": "tool", "tool_name": "run_command",
                                             "tool_info": {"name": "run_command", "parameters": {"CommandLine": "echo B3"}}}},
    {"event": "step_update", "step_update": {"conversation_id": agy.CONV, "step_index": 4, "state": "ERROR",
                                             "step_type": "tool", "tool_name": "run_command", "tool_info": {
                                                 "name": "run_command", "parameters": {"CommandLine": "echo B3"},
                                                 "error": {"type": "TOOL_ERROR", "message": "tool call denied by "
                                                           "pre-tool hook: XO-REASON-B3"}}}},
    {"event": "step_update", "step_update": {"conversation_id": agy.CONV, "step_index": 5, "state": "ACTIVE",
                                             "step_type": "agent_response", "text_delta": "Green"}},
    {"event": "step_update", "step_update": {"conversation_id": agy.CONV, "step_index": 5, "state": "DONE",
                                             "step_type": "agent_response", "text_delta": ".\n",
                                             "usage": {"input_tokens": 7702, "output_tokens": 25}}},
    {"event": "result", "result": {"conversation_id": agy.CONV, "status": "SUCCESS", "response": "Green.\n",
                                   "num_turns": 1, "usage": {"input_tokens": 15310, "output_tokens": 62,
                                                             "thinking_tokens": 0, "cache_read_tokens": 0}}},
]


def names(outs) -> list[str]:
    return [o.cls.__name__ for o in outs]


class WireTests(unittest.TestCase):
    def wire(self) -> Wire:
        w = Wire(cwd="/work")
        w.conversation = agy.CONV
        return w

    def test_a_live_turn(self) -> None:
        w = self.wire()
        turn = w.expect_turn("person")
        outs = [o for line in LIVE_TURN for o in w.line(line)]
        self.assertEqual(names(outs), ["TurnStarted", "Usage", "ToolStarted", "ToolFinished", "ToolStarted",
                                       "ToolFinished", "TextDelta", "TextDelta", "Usage", "TextDone", "TurnFinished"])
        self.assertTrue(all(o.fields["turn_id"] == turn for o in outs))
        tools_ = [o.fields for o in outs if o.cls is c.ToolStarted]
        self.assertEqual([(t["call_id"], t["kind"], t["title"]) for t in tools_],
                         [(f"{agy.CONV}:2", "command", "Run `echo A1`"), (f"{agy.CONV}:4", "command", "Run `echo B3`")])
        self.assertEqual([o.fields["status"] for o in outs if o.cls is c.ToolFinished], ["ok", "denied"])
        done = outs[-1].fields
        self.assertEqual((done["status"], done["text"]), ("completed", "Green."))
        # the first turn of a process: its own steps' tokens; then the result's total is the baseline
        self.assertEqual((done["usage"].input, done["usage"].output), (15310, 62))
        self.assertEqual(w.baseline.input, 15310)

    def test_the_next_turn_is_the_difference_from_the_last_result(self) -> None:
        w = self.wire()
        w.expect_turn("person")
        for line in LIVE_TURN:
            w.line(line)
        w.expect_turn("person")
        w.line(LIVE_TURN[0])
        outs = w.line({"event": "result", "result": {"status": "SUCCESS", "response": "ok", "usage": {
            "input_tokens": 23000, "output_tokens": 70}}})
        self.assertEqual((outs[-1].fields["usage"].input, outs[-1].fields["usage"].output), (7690, 8))

    def test_sends_take_turns_in_order_and_a_turn_agy_starts_itself_is_injected(self) -> None:
        w = self.wire()
        first, second = w.expect_turn("person"), w.expect_turn("wake")
        self.assertEqual(w.line(LIVE_TURN[0])[0].fields, {"origin": "person", "turn_id": first})
        w.line(LIVE_TURN[-1])
        self.assertEqual(w.line(LIVE_TURN[0])[0].fields, {"origin": "wake", "turn_id": second})
        w.line(LIVE_TURN[-1])
        # a sub-agent's report wakes the root after its turn ended
        outs = w.line({"event": "step_update", "step_update": {"conversation_id": agy.CONV, "step_index": 9,
                                                               "state": "DONE", "step_type": "system_message"}})
        self.assertEqual(outs[0].fields["origin"], "injected")

    def test_an_empty_answer_after_agys_own_refusal_says_so(self) -> None:
        w = self.wire()
        w.expect_turn("person")
        w.line(LIVE_TURN[0])
        outs = w.line({"event": "result", "result": {"status": "SUCCESS", "response": "", "denied_actions": [
            {"action": "command", "display_name": "RunCommand"}], "usage": {"input_tokens": 1, "output_tokens": 1}}})
        self.assertEqual(outs[-1].fields["status"], "completed")
        self.assertIn("RunCommand", outs[-1].fields["error"])

    def test_an_interrupt_ends_the_turn_interrupted_and_cancels_open_calls(self) -> None:
        w = self.wire()
        w.expect_turn("person")
        w.line(LIVE_TURN[0])
        w.line(LIVE_TURN[2])
        outs = w.line({"event": "result", "result": {"status": "ERROR", "error": "interrupted", "response": ""}})
        self.assertEqual(names(outs), ["ToolFinished", "TurnFinished"])
        self.assertEqual((outs[0].fields["status"], outs[1].fields["status"], outs[1].fields["error"]),
                         ("cancelled", "interrupted", None))

    def test_a_sub_agent_starts_a_run_that_goes_on_in_the_background(self) -> None:
        w = self.wire()
        w.expect_turn("person")
        w.line(LIVE_TURN[0])
        spawn = {"event": "step_update", "step_update": {
            "conversation_id": agy.CONV, "step_index": 2, "state": "DONE", "step_type": "subagent",
            "tool_name": "invoke_subagent", "subagent_info": {"subagents": [{
                "type_name": "self", "role": "Shell Task Runner", "conversation_id": CHILD,
                "initial_prompt": "Run it"}]}}}
        outs = w.line(spawn)
        self.assertEqual(names(outs), ["ToolStarted", "ToolFinished", "RunStarted"])
        info = outs[-1].fields["info"]
        self.assertEqual((info.run_id, info.title, info.spawn_call_id), (CHILD, "Shell Task Runner", f"{agy.CONV}:2"))
        ended = w.line(LIVE_TURN[-1])
        self.assertEqual(names(ended), ["RunProgress", "TurnFinished"])
        self.assertTrue(ended[0].fields["background"])
        self.assertEqual(names(w.finish_run(CHILD, "completed", "done")), ["RunFinished"])

    def test_unknown_data_is_a_notice_once(self) -> None:
        w = self.wire()
        self.assertEqual(names(w.line({"event": "brand_new"})), ["Notice"])
        self.assertEqual(w.line({"event": "brand_new"}), [])


class ToolTests(unittest.TestCase):
    def test_both_names_agy_gives_its_file_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("write_file", "write_to_file"):
                call = tools.describe(name, {"Path": "a.txt", "Content": "hi"}, tmp)
                self.assertEqual((call.kind, call.title, call.paths), ("create_file", "Write `a.txt`",
                                                                       (os.path.join(tmp, "a.txt"),)))
            Path(tmp, "a.txt").write_text("x")
            self.assertEqual(tools.describe("write_file", {"Path": "a.txt"}, tmp).kind, "edit_file")
            for name in ("edit_file", "replace_file_content", "multi_replace_file_content"):
                self.assertEqual(tools.describe(name, {"TargetFile": "a.txt"}, tmp).kind, "edit_file")
            self.assertEqual(tools.describe("read_file", {"Path": "a.txt"}, tmp).kind, "read")
            self.assertEqual(tools.describe("view_file", {"AbsolutePath": "/x/a.txt"}, tmp).kind, "read")

    def test_commands_mcp_web_and_the_rest(self) -> None:
        run = tools.describe("run_command", {"CommandLine": "git push", "toolSummary": "Push the branch"}, "/w")
        self.assertEqual((run.kind, run.title, run.command), ("command", "Push the branch", "git push"))
        mcp = tools.describe("call_mcp_tool", {"ServerName": "xo_xo_comms", "ToolName": "reply",
                                               "Arguments": {"text": "hi"}}, "/w")
        self.assertEqual((mcp.kind, mcp.mcp, mcp.title), ("mcp", ("xo_xo_comms", "reply"), "`reply` on `xo_xo_comms`"))
        self.assertEqual(tools.describe("read_url_content", {"Url": "https://x.y"}, "/w").url, "https://x.y")
        self.assertEqual(tools.describe("invoke_subagent", {"Role": "Reviewer"}, "/w").kind, "subagent")
        self.assertEqual(tools.describe("brand_new_tool", {}, "/w").kind, "other")

    def test_protected_paths_own_edits_and_agys_own_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(tools.protected_any([os.path.join(tmp, ".agents", "hooks.json")], tmp), ".agents/**")
            self.assertEqual(tools.protected_any(["/elsewhere/proj/.agents/plugins.json"], tmp), ".agents/**")
            self.assertEqual(tools.protected_any([os.path.join(tmp, ".git", "hooks", "pre-commit")], tmp), ".git/**")
            self.assertIsNone(tools.protected_any([os.path.join(tmp, "src", "a.py")], tmp))
            self.assertTrue(tools.own_edit([os.path.join(tmp, "a.py")], tmp))
            self.assertFalse(tools.own_edit(["/etc/passwd"], tmp))
            os.symlink("/etc", os.path.join(tmp, "out"))
            self.assertFalse(tools.own_edit([os.path.join(tmp, "out", "x")], tmp))   # a link can't escape
            self.assertTrue(tools.artifact_write([f"/b/{agy.CONV}/plan.md"], f"/b/{agy.CONV}"))
            self.assertFalse(tools.artifact_write([f"/b/{agy.CONV}/../x"], f"/b/{agy.CONV}"))


class ArgvTests(unittest.TestCase):
    def test_the_command_line(self) -> None:
        spec = c.SessionSpec(cwd="/w", model="gemini-3.8-flash-low", effort="low", add_dirs=("/d",),
                             permissions=c.PermissionPolicy(mode="plan"))
        self.assertEqual(a.argv("agy", spec, mode="plan", resume=agy.CONV, model=spec.model, effort="low",
                                schema_file="/s.json"),
                         ["agy", "--input-format", "stream-json", "--output-format", "stream-json", "--model",
                          "gemini-3.8-flash-low", "--effort", "low", "--mode", "plan", "--conversation", agy.CONV,
                          "--add-dir", "/d", "--json-schema", "/s.json", "--print="])

    def test_no_mode_ever_writes_a_skip_flag(self) -> None:
        for mode in a.MODES:
            argv = argv_for(c.SessionSpec(cwd="/w", permissions=c.PermissionPolicy(mode=mode)))
            self.assertFalse([x for x in argv if any(f in x for f in SKIP_FLAGS)])
            self.assertEqual(argv[-1], "--print=")


class AccountTests(unittest.TestCase):
    def test_usage_rows(self) -> None:
        windows = account.parse_usage("noise\n" + agy.USAGE)
        self.assertEqual([(w.group, w.name, w.used) for w in windows][:2],
                         [("Gemini Models", "weekly", 0.0), ("Gemini Models", "five_hour", 0.01)])
        guard = account.UsageGuard()
        guard.update(account.parse_usage("Gemini Models\tFive Hour Limit Remaining\t0%\t2999-01-01T00:00:00Z\n"
                                         "Claude and GPT models\tFive Hour Limit Remaining\t50%\t2999-01-01T00:00:00Z"))
        with self.assertRaises(c.RuntimeFailure) as caught:
            guard.check("gemini-3.8-flash-low")
        self.assertEqual(caught.exception.code, "rate_limited")
        guard.check("claude-sonnet-4-6")                      # another group's quota
        self.assertEqual(guard.worst("claude-sonnet-4-6").used, 0.5)

    def test_a_token_refresh_is_not_a_new_login(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            token = Path(home, ".gemini", "antigravity-cli", "antigravity-oauth-token")
            token.parent.mkdir(parents=True)

            def write(access: str, email: str = "a@b.c") -> None:
                token.write_text(json.dumps({"auth_method": "consumer", "id_token_email": email,
                                             "token": {"access_token": access, "refresh_token": "r"}}))
            env = {"HOME": home}
            write("one")
            first = account.login_fingerprint(env)
            write("two")
            self.assertEqual(account.login_fingerprint(env), first)
            write("two", email="x@y.z")
            self.assertNotEqual(account.login_fingerprint(env), first)
            self.assertTrue(account.logged_in(env))

    def test_quota_errors(self) -> None:
        error = account.agy_error('AGY_ERROR: {"code": "RESOURCE_EXHAUSTED", "retryable": true}')
        self.assertTrue(error and account.is_quota(error))
        self.assertIsNone(account.agy_error("something else"))


class TranscriptTests(unittest.TestCase):
    def test_a_conversation_and_its_sub_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            parent, child = write_transcripts(home)
            brain = home / ".gemini" / "antigravity-cli" / "brain"
            runs = tr.list_runs(parent, brain)
            self.assertEqual([(r.run_id, r.title, r.spawn_call_id) for r in runs],
                             [(child, "Build Checker", f"{parent}:2")])
            items = tr.read_run(parent, child, limit=100, home=brain).items
            self.assertEqual([i.type for i in items], ["input", "thinking", "tool_call", "tool_call", "tool_result",
                                                       "tool_result", "tool_call", "tool_result", "text"])
            self.assertEqual(items[0].text, "Run make and report.")
            self.assertEqual([(i.call_id, i.status) for i in items if i.type == "tool_result"],
                             [(f"{child}:2", "ok"), (f"{child}:3", "denied"), (f"{child}:5", "ok")])
            with self.assertRaises(c.RuntimeFailure) as caught:
                tr.read(parent, after="nowhere", home=brain)
            self.assertEqual(caught.exception.code, "protocol")
            with self.assertRaises(c.RuntimeFailure) as caught:
                tr.read("00000000-0000-4000-8000-000000000000", home=brain)
            self.assertEqual(caught.exception.code, "session_not_found")

    def test_the_tail_reads_whole_lines_as_they_come(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "t.jsonl")
            path.write_text('{"step_index": 0, "type": "A"}\n{"step_ind')
            tail = tr.Tail(path)
            self.assertEqual([s["type"] for s in tail.read()], ["A"])
            with path.open("a") as f:
                f.write('ex": 1, "type": "B"}\n')
            self.assertEqual([s["type"] for s in tail.read()], ["B"])
            self.assertEqual(tail.read(), [])


if __name__ == "__main__":
    unittest.main()
