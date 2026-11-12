"""A turn ends at ``no_reply_needed`` (storm plan W5). Once comms has
recorded the agent's own ending call and its CLI has the result, whatever
the model goes on with in that turn (words, thinking, a tool call) is
interrupted at once, and comms refuses the session's calls until its next
turn. On the fake claude CLI, through the real ``xo_comms`` endpoint; the
11-10 connector-notice turn is replayed from its recorded transcript
(``tests/fixtures/storm/connector-notice-2026-11-10.json``)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from services.cowork_agent.storm.comms.calls import TURN_ENDED
from services.cowork_agent.storm.engine.session import TURN_ENDED as STOPPED_BY
from services.cowork_agent.storm.presets import Intelligence
from tests.fakes import claude
from tests.storm_engine_kit import SID, Kit, message
from tests.test_storm_chats_flows import PC, TH, FlowTestCase, done, next_input, start

FIXTURE = Path(__file__).parent / "fixtures" / "storm" / "connector-notice-2026-11-10.json"
#: How claude names an ``xo_comms`` tool.
COMMS = "mcp__xo_comms__"


def called(sid: str, call_id: str, tool: str, arguments: dict[str, Any], *, message_id: str) -> list[dict]:
    """One comms call as claude makes it: the tool call, the call itself
    over HTTP, then its result back to the model."""
    return [claude.tool_call(sid, call_id, COMMS + tool, arguments, message_id=message_id),
            claude.mcp_call(tool, arguments), claude.tool_result(sid, call_id, json.dumps({"ok": True}))]


def first_output(steps: list[dict]) -> int:
    """The index of the first step that is model output: a text or thinking
    delta, or a tool call."""
    for at, step in enumerate(steps):
        line = step.get("emit") or {}
        delta = (line.get("event") or {}).get("delta") or {}
        content = (line.get("message") or {}).get("content") or []
        if delta.get("type") in ("text_delta", "thinking_delta") or (
                line.get("type") == "assistant" and any(b.get("type") == "tool_use" for b in content)):
            return at
    raise AssertionError("no model output in these steps")


def replay(sid: str) -> tuple[list[dict], list[dict]]:
    """The recorded turn as claude would print it, one model message at a
    time (its tool calls, then their results): up to the ending call's
    result, then everything after it. The connector notices are inside the
    CLI (its next request to the model), so nothing of them is printed."""
    entries = json.loads(FIXTURE.read_text(encoding="utf-8"))["turn"]
    results = {e["id"]: e for e in entries if e["kind"] == "tool_result"}
    messages: list[tuple[str, list[dict]]] = []
    for entry in entries:
        if "message" not in entry:
            continue
        if not messages or messages[-1][0] != entry["message"]:
            messages.append((entry["message"], []))
        messages[-1][1].append(entry)
    played: list[list[dict]] = []
    for message_id, blocks in messages:
        steps = claude.streamed(sid, message_id, [
            ("tool_use", b["id"], b["name"], b["input"]) if b["kind"] == "tool_use" else (b["kind"], b["text"])
            for b in blocks])
        for call in (b for b in blocks if b["kind"] == "tool_use"):
            answer = results[call["id"]]
            steps += [claude.mcp_call(call["name"].removeprefix(COMMS), call["input"]),
                      claude.tool_result(sid, call["id"], answer["content"], is_error=answer.get("is_error", False))]
        played.append(steps)
    return played[0], [step for steps in played[1:] for step in steps]


class TurnEndTests(FlowTestCase):
    def interrupts(self, run: dict) -> int:
        return sum(1 for line in run["stdin"] if (line.get("request") or {}).get("subtype") == "interrupt")

    def calls(self) -> list[tuple]:
        """Every comms call recorded: tool, ok, ends_turn, error."""
        return self.rows("SELECT json_extract(data, '$.tool'), json_extract(data, '$.ok'), "
                         "json_extract(data, '$.ends_turn'), json_extract(data, '$.error') FROM events "
                         "WHERE type = 'comms.call' ORDER BY seq")

    def stops(self) -> list[tuple]:
        return self.rows("SELECT json_extract(data, '$.by') FROM events WHERE type = 'turn.stopped'")

    async def cut_off(self, late: list[dict]) -> tuple[dict, dict]:
        """A coordinator ends its turn with ``no_reply_needed``, then its
        model goes on with ``late``: returns its run and its chat ids."""
        await self.boot_roles({PC: [*start("pc"), *called("pc", "toolu_end", "no_reply_needed", {"reason": "seen"},
                                                          message_id="msg_end"),
                                    claude.interruptible("pc", [*late, claude.result("pc", "ok")]),
                                    claude.idle("pc")]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Status?")
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended'")[0][0] == 1)
        (run,) = self.kit.runs()
        return run, ids

    async def test_text_then_a_tool_call_after_no_reply_needed_is_cut_off_at_its_first_word(self) -> None:
        late = [*claude.streamed("pc", "msg_late", [
            ("text", "Pausing the graders as you asked."),
            ("tool_use", "toolu_late", COMMS + "post_message", {"text": "Paused the graders."})]),
            claude.mcp_call("post_message", {"text": "Paused the graders."}),
            claude.tool_result("pc", "toolu_late", json.dumps({"message_id": "cmsg_x"}))]
        run, ids = await self.cut_off(late)
        # Interrupted right after the first word it streamed: the tool call never came, nor its comms call.
        self.assertEqual(run["cut"]["after"], first_output(late))
        self.assertEqual(run["cut"]["skipped"], len(late) - first_output(late))
        self.assertEqual(self.interrupts(run), 1)
        self.assertEqual([m["tool"] for m in run["mcp"]], ["no_reply_needed"])
        self.assertEqual(self.calls(), [("no_reply_needed", 1, 1, None)])
        self.assertEqual(self.texts(ids["demo"]), [("owner", "Status?")])
        # The turn's record says why.
        self.assertEqual(self.rows("SELECT status, stop_reason FROM turns"), [("stopped", "interrupted")])
        self.assertEqual(self.stops(), [(STOPPED_BY,)])

    async def test_a_tool_call_after_no_reply_needed_is_cut_off_before_it_runs(self) -> None:
        late = called("pc", "toolu_late", "list_thread_sessions", {}, message_id="msg_late")
        run, _ids = await self.cut_off(late)
        self.assertEqual((run["cut"]["after"], first_output(late)), (0, 0))
        self.assertEqual([m["tool"] for m in run["mcp"]], ["no_reply_needed"])
        self.assertEqual(self.stops(), [(STOPPED_BY,)])

    async def test_the_connector_notice_turn_now_produces_nothing(self) -> None:
        """The 11-10 20:16 turn, replayed: after ``no_reply_needed``, the
        model's next reply began with the invented request ("Pausing the
        graders as you asked"), then acted on it through comms. Now that
        reply is cut off at its first thinking: no call, no message, no hop."""
        ended, late = replay("pc")
        await self.boot_roles({PC: [*start("pc"), *ended,
                                    claude.interruptible("pc", [*late, claude.result("pc", "ok")]),
                                    claude.idle("pc")]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Status?")
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended'")[0][0] == 1)
        (run,) = self.kit.runs()
        self.assertEqual(run["cut"]["after"], first_output(late))
        self.assertEqual(late[first_output(late)]["emit"]["event"]["delta"]["thinking"], "Pausing ")
        self.assertEqual([m["tool"] for m in run["mcp"]], ["no_reply_needed"])
        self.assertEqual(self.calls(), [("no_reply_needed", 1, 1, None)])
        self.assertEqual(self.texts(ids["demo"]), [("owner", "Status?")])
        self.assertEqual(self.rows("SELECT count(*) FROM hops"), [(0,)])
        self.assertEqual(self.stops(), [(STOPPED_BY,)])

    async def test_a_late_call_in_the_same_turn_is_refused_and_the_next_turn_starts_clean(self) -> None:
        """Two calls in one go (the CLI hasn't handed back the first's
        result): the one after ``no_reply_needed`` is refused. The text
        after them isn't cut, since the CLI never had the result."""
        await self.boot_roles({PC: [*start("pc"), claude.mcp_call("no_reply_needed", {"reason": "seen"}),
                                    claude.mcp_call("post_message", {"text": "Paused the graders."}), *done("pc"),
                                    next_input(), claude.mcp_call("post_message", {"text": "All green."}),
                                    *done("pc", 2)]})
        ids = await self.chat_ids()
        await self.post(ids["demo"], "Status?")
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended'")[0][0] == 1)
        await self.post(ids["demo"], "And now?")
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended'")[0][0] == 2)
        (run,) = self.kit.runs()
        self.assertEqual([(m["tool"], m.get("error")) for m in run["mcp"]],
                         [("no_reply_needed", None), ("post_message", TURN_ENDED), ("post_message", None)])
        self.assertEqual(self.calls(), [("no_reply_needed", 1, 1, None), ("post_message", 0, None, "turn_ended"),
                                        ("post_message", 1, None, None)])
        self.assertEqual(self.texts(ids["demo"]),
                         [("owner", "Status?"), ("owner", "And now?"), ("agent", "All green.")])
        self.assertEqual(self.interrupts(run), 0)
        self.assertEqual(self.rows("SELECT status FROM turns ORDER BY started_at"), [("finished",), ("finished",)])

    async def test_text_after_reply_goes_on(self) -> None:
        """Only ``no_reply_needed`` ends a turn: a thread's words after its
        ``reply`` stream to the end."""
        after = claude.streamed("th", "msg_after", [("text", "Replied; tidying up my notes.")])
        await self.boot_roles({
            PC: [*start("pc"), claude.mcp_call("no_reply_needed", {"reason": "status"}), *done("pc")],
            TH: [*start("th"), *called("th", "toolu_reply", "reply", {"text": "Fixed."}, message_id="msg_reply"),
                 claude.interruptible("th", [*after, claude.result("th", "ok")], pace=0.1), claude.idle("th")]})
        chat = await self.project()
        root = await self.owner_message(chat, "Fix the login page")

        def wake(w, session_id: str, _note: str | None) -> str:
            w.wake(session_id, message(1, "Fix the login page"))
            return session_id
        thread = await self.chats.start_thread(chat, root["id"], "Fix login", Intelligence(), wake)
        await self.settle(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND session_id = ?",
                                            (thread,))[0][0] == 1)
        (run,) = [r for r in self.kit.runs() if any(m["tool"] == "reply" for m in r.get("mcp", []))]
        self.assertNotIn("cut", run)
        self.assertEqual(self.interrupts(run), 0)
        self.assertEqual(self.rows("SELECT status FROM turns WHERE session_id = ?", (thread,)), [("finished",)])
        self.assertEqual(self.stops(), [])


class SteeredInputTests(unittest.IsolatedAsyncioTestCase):
    """An input steered into a turn its agent had ended is new work for the
    model: its answer is never cut off."""

    async def asyncSetUp(self) -> None:
        self.kit = Kit()

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    async def test_an_input_added_after_the_ending_call_reopens_the_turn(self) -> None:
        kit = self.kit
        kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID),
                      claude.tool_call(SID, "toolu_end", COMMS + "no_reply_needed", {"reason": "seen"}),
                      claude.user_message(),                   # the input added to the running turn
                      claude.tool_result(SID, "toolu_end", json.dumps({"ok": True})),
                      claude.interruptible(SID, claude.text_turn(SID, "On it.", message_id="msg_2"), pace=0.1),
                      claude.idle(SID)])
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        step = await kit.recorder.seen("step.tool", session_id=sid)
        # What comms records for the agent's own ending call (a plain session has no comms of its own).
        await engine.write(lambda w: w.append("comms.call", {"tool": "no_reply_needed", "ok": True, "ends_turn": True},
                                              session_id=sid, turn_id=step.turn_id, epoch=1))
        self.assertTrue(engine.turn_over(sid, 1))
        await engine.deliver(sid, message(2, "and one more thing", delivery="add"))
        finished = await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual((finished.data["status"], finished.data["wakes"]), ("finished", ["msg:cmsg_1", "msg:cmsg_2"]))
        (run,) = kit.runs()
        self.assertNotIn("cut", run)
        self.assertFalse(engine.turn_over(sid, 1))
