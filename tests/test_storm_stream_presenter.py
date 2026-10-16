"""The turn presenter and its wording table (ch 09 §2, §2.1), as a pure
fold: the same stored events always give the same blocks, whatever came
live in between."""

from __future__ import annotations

import unittest

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.stream import BLOCK_TYPES, TurnPresenter, present, wording
from services.cowork_agent.storm.stream.presenter import Streams
from services.cowork_agent.storm.stream.turns import Turns

TURN, TS = "turn_1", "2026-10-15T10:00:00Z"


class Turn:
    """Stored events for one turn, and live ones, numbered in order."""

    def __init__(self) -> None:
        self.events: list[StoredEvent] = []
        self.n = 0

    def stored(self, type_: str, **data) -> StoredEvent:
        self.n += 1
        event = StoredEvent(self.n, TS, type_, data, session_id="cse_1", turn_id=TURN)
        self.events.append(event)
        return event

    def tool(self, call: str, kind: str, title: str, target: str | None = None, tool: str = "Tool") -> StoredEvent:
        return self.stored("step.tool", call_id=call, tool=tool, kind=kind, title=title, target=target)

    def done(self, call: str, status: str = "ok", **more) -> StoredEvent:
        return self.stored("step.tool_done", call_id=call, status=status, **more)

    @staticmethod
    def live(cls: type[c.Event], **fields) -> c.Event:
        return cls(session="cse_1", ts=TS, seq=0, turn_id=TURN, **fields)


def fold(events, presenter: TurnPresenter | None = None) -> list[dict]:
    presenter = presenter or TurnPresenter(TURN)
    finished = {}
    for event in events:
        for out in presenter.durable(event):
            if out.type == "block.finished":
                finished[out.data["block"]["id"]] = out.data["block"]
    return sorted(finished.values(), key=lambda b: b["index"])


class WordingTests(unittest.TestCase):
    def test_every_kind_has_one_row_wording_running_and_done(self) -> None:
        cases = [
            ("create_file", "/w/app.py", "Writing `app.py`", "Created `app.py`"),
            ("edit_file", "/w/app.py", "Editing `app.py`", "Edited `app.py`"),
            ("delete_file", "/w/old.py", "Deleting `old.py`", "Deleted `old.py`"),
            ("read", "/w/README.md", "Reading `README.md`", "Read `README.md`"),
            ("search", "TODO", "Searching for `TODO`", "Searched for `TODO`"),
            ("list", "src", "Searching for `src`", "Searched for `src`"),
            ("command", "pytest -q", "Run the tests", "Run the tests · done"),
            ("web_fetch", "https://docs.python.org/3/", "Reading `docs.python.org`", "Read `docs.python.org`"),
            ("web_search", "sqlite wal", "Searching the web for sqlite wal", "Searched the web for sqlite wal"),
            ("mcp", "github/create_issue", "Run the tests", "Run the tests"),     # the runtime's own title
            ("subagent", "Explore", "Sub-agent: `Run the tests`", "Sub-agent: `Run the tests`"),
            ("ask", None, "Asking you: Run the tests", "Asked you: Run the tests"),
            ("other", None, "Frobnicate", "Frobnicate"),
        ]
        self.assertEqual({k for k, *_ in cases} | {wording.PLAN_KIND}, set(c.ToolKind.__args__))
        self.assertEqual(set(wording.WORDING) - set(c.ToolKind.__args__), {"notice", "error"})
        for kind, target, running, done in cases:
            with self.subTest(kind=kind):
                tool = "Frobnicate" if kind == "other" else "Tool"
                title = "Run the tests"
                self.assertEqual(wording.row_line(kind, tool=tool, title=title, target=target, status="running"),
                                 running)
                self.assertEqual(wording.row_line(kind, tool=tool, title=title, target=target, status="ok"), done)
                counted = kind != "ask"
                self.assertEqual(wording.row_line(kind, tool=tool, title=title, target=target, status="denied"),
                                 f"{title} · denied" if counted else title)

    def test_a_file_row_grows_while_it_is_written(self) -> None:
        self.assertEqual(wording.row_line("create_file", tool="Write", title="Write `app.py`", target="/w/app.py",
                                          status="running", size=3482), "Writing `app.py` · 3.4 KB")
        self.assertEqual(wording.row_line("create_file", tool="Write", title="Write `app.py`", target="/w/app.py",
                                          status="ok", size=3482), "Created `app.py`")

    def test_a_group_header_counts_by_verb_in_order(self) -> None:
        rows = [("create_file", "ok"), ("command", "ok"), ("create_file", "ok"), ("search", "ok"), ("list", "ok"),
                ("web_fetch", "ok"), ("web_search", "ok"), ("other", "ok"), ("ask", "ok"), ("command", "denied"),
                ("read", "error"), ("edit_file", "running")]
        self.assertEqual(wording.group_header(rows), "Created 2 files, ran a command, searched 2 times, looked up "
                                                     "2 pages, did another step, 1 step failed, 1 step denied")
        self.assertEqual(wording.group_header([("search", "ok")]), "Searched once")
        self.assertEqual(wording.group_header([("edit_file", "running")], running_line="Editing `a.py`"),
                         "Editing `a.py`")

    def test_thinking_and_plan_headers(self) -> None:
        self.assertEqual(wording.thinking_header(step_title=None, duration_ms=None, done=False), "Thinking…")
        self.assertEqual(wording.thinking_header(step_title="Planning the cache", duration_ms=None, done=False),
                         "Planning the cache")
        self.assertEqual(wording.thinking_header(step_title=None, duration_ms=32_400, done=True), "Thought for 32s")
        self.assertEqual(wording.plan_header([("a", "done"), ("b", "doing")]), "Plan · 1 of 2 done")

    def test_shared_files_is_a_block_type_ready_for_comms(self) -> None:
        self.assertIn("shared_files", BLOCK_TYPES)


class FoldTests(unittest.TestCase):
    def test_text_is_narration_when_work_follows_and_the_answer_when_nothing_does(self) -> None:
        t = Turn()
        t.stored("turn.started", origin="person")
        t.stored("step.text", block_id="b1", text="I'll look.")
        t.tool("r1", "read", "Read `a`", "/w/a")
        t.done("r1")
        t.stored("step.text", block_id="b2", text="First part.")
        t.stored("step.text", block_id="b3", text="It says hi.")
        t.stored("turn.finished", status="finished")
        self.assertEqual([(b["type"], b.get("text") or b.get("header")) for b in present(t.events)],
                         [("narration", "I'll look."), ("activity", "Read a file"), ("narration", "First part."),
                          ("answer", "It says hi.")])

    def test_calls_group_between_text_and_thinking_and_a_plan_call_is_the_plan_block(self) -> None:
        t = Turn()
        t.stored("turn.started", origin="person")
        t.tool("a", "read", "Read `a`", "/w/a")
        t.done("a")
        t.tool("p", "plan", "Update the plan")
        t.stored("step.plan", items=[{"text": "x", "state": "doing"}])
        t.done("p")
        t.tool("b", "command", "List files", "ls")
        t.done("b")
        t.stored("step.thinking", block_id="th", steps=["Hmm."], duration_ms=2000)
        t.tool("c", "read", "Read `c`", "/w/c")
        t.done("c")
        t.stored("step.plan", items=[{"text": "x", "state": "done"}])
        t.stored("turn.finished", status="finished")
        blocks = present(t.events)
        self.assertEqual([(b["id"], b["type"]) for b in blocks], [
            ("turn_1:g0", "activity"), ("turn_1:plan", "plan"), ("th", "thinking"), ("turn_1:g1", "activity")])
        self.assertEqual([r["call_id"] for r in blocks[0]["rows"]], ["a", "b"])       # the plan call is no row
        self.assertEqual(blocks[1]["items"], [{"text": "x", "state": "done"}])        # updated in place
        self.assertEqual(blocks[2]["header"], "Thought for 2s")

    def test_a_group_waits_for_its_last_row_and_a_row_that_never_ended_is_cancelled(self) -> None:
        t = Turn()
        t.stored("turn.started", origin="person")
        t.tool("a", "command", "Build", "make")
        presenter = TurnPresenter(TURN)
        for event in t.events:
            presenter.durable(event)
        outs = presenter.durable(t.stored("step.text", block_id="b1", text="Waiting on the build."))
        self.assertEqual([o.type for o in outs], ["block.started"])                   # the group is still open
        outs = presenter.durable(t.stored("turn.finished", status="failed"))
        group = next(o.data["block"] for o in outs if o.data["block"]["type"] == "activity")
        self.assertEqual(group["rows"][0]["status"], "cancelled")
        self.assertEqual(group["header"], "1 step cancelled")

    def test_bytes_before_the_call_starts_open_the_group_the_record_will_open(self) -> None:
        t = Turn()
        live = TurnPresenter(TURN, streams=Streams(input_bytes=True))
        live.durable(t.stored("turn.started", origin="person"))
        live.durable(t.stored("step.text", block_id="b1", text="Writing it."))
        outs = live.ephemeral(Turn.live(c.ToolInputDelta, call_id="w1", bytes_so_far=1500))
        self.assertEqual([(o.type, o.data["block"]["id"], o.data["block"]["index"]) for o in outs],
                         [("block.started", "turn_1:g0", None)])
        self.assertEqual(outs[0].data["block"]["rows"][0]["line"], "Writing… · 1.5 KB")
        outs = live.durable(t.tool("w1", "create_file", "Write `a.py`", "/w/a.py", tool="Write"))
        self.assertEqual([(o.type, o.data["block"]["id"]) for o in outs],
                         [("block.finished", "b1"), ("block.started", "turn_1:g0")])
        (group,) = live.open_blocks()
        self.assertEqual((group["index"], group["rows"][0]["line"]), (1, "Writing `a.py` · 1.5 KB"))
        live.durable(t.done("w1", size=1600))
        live.durable(t.stored("turn.finished", status="finished"))
        self.assertTrue(live.ended)
        self.assertEqual(fold(t.events), present(t.events))

    def test_what_streams_never_changes_what_the_record_gives(self) -> None:
        t = Turn()
        t.stored("turn.started", origin="person")
        live = TurnPresenter(TURN, streams=Streams(thinking_text=True, input_bytes=True, output=True))
        finished: dict[str, dict] = {}

        def apply(event) -> None:
            for out in live.durable(event):
                if out.type == "block.finished":
                    finished[out.data["block"]["id"]] = out.data["block"]
        apply(t.events[0])
        for piece in ("I'm reading. ", "\n\nThen fixing."):
            live.ephemeral(Turn.live(c.ThinkingDelta, block_id="th", delta=piece, step_title=piece.strip()))
        apply(t.stored("step.thinking", block_id="th", steps=["I'm reading.", "Then fixing."], duration_ms=900))
        apply(t.tool("x", "command", "Run it", "./run"))
        live.ephemeral(Turn.live(c.ToolOutputDelta, call_id="x", delta="line 1\n"))
        live.ephemeral(Turn.live(c.TextDelta, block_id="gone", delta="half"))
        apply(t.done("x", summary="line 1"))
        apply(t.stored("turn.finished", status="interrupted"))
        self.assertEqual(sorted(finished.values(), key=lambda b: b["index"]), present(t.events))

    def test_a_sub_agents_own_steps_and_other_turns_are_not_this_turns_blocks(self) -> None:
        live = TurnPresenter(TURN)
        live.durable(StoredEvent(1, TS, "turn.started", {}, turn_id=TURN))
        self.assertEqual(live.ephemeral(Turn.live(c.TextDelta, block_id="s", delta="x", run="task_1")), [])
        self.assertEqual(live.durable(StoredEvent(2, TS, "step.text", {"block_id": "o", "text": "x"},
                                                  turn_id="turn_2")), [])
        self.assertEqual(live.open_blocks(), [])


class DegradeTests(unittest.TestCase):
    def test_claudes_feature_list_says_what_streams_live(self) -> None:
        streams = Streams.from_features(ClaudeRuntime().features().features)
        self.assertEqual(streams, Streams(thinking_text=True, input_bytes=True, output=False))
        self.assertEqual(Streams.from_features({}), Streams())

    def test_without_thinking_text_the_header_stays_thinking_until_the_steps_land(self) -> None:
        live = TurnPresenter(TURN, streams=Streams())
        live.durable(StoredEvent(1, TS, "turn.started", {}, turn_id=TURN))
        live.ephemeral(Turn.live(c.ThinkingDelta, block_id="th", delta="", step_title="Reading the code"))
        (block,) = live.open_blocks()
        self.assertEqual((block["header"], block["steps"], block["streams_text"]), ("Thinking…", [], False))

    def test_a_running_row_says_whether_its_size_or_output_will_stream(self) -> None:
        for streams, expected in ((Streams(input_bytes=True), ("bytes", None)), (Streams(output=True),
                                                                                (None, "output"))):
            with self.subTest(streams=streams):
                live = TurnPresenter(TURN, streams=streams)
                t = Turn()
                live.durable(t.stored("turn.started"))
                live.durable(t.tool("w", "create_file", "Write `a`", "/w/a"))
                live.durable(t.tool("x", "command", "Run it", "./run"))
                (group,) = live.open_blocks()
                self.assertEqual(tuple(r["streams"] for r in group["rows"]), expected)


class MoreWordingTests(unittest.TestCase):
    """ch 09 §2, §2.1 as written."""

    def test_a_command_ends_with_how_it_exited(self) -> None:
        line = lambda status: wording.row_line("command", tool="Bash", title="Run the tests", target="pytest",
                                               status=status)
        self.assertEqual([line(s) for s in ("running", "ok", "error", "cancelled")],
                         ["Run the tests", "Run the tests · done", "Run the tests · failed",
                          "Run the tests · cancelled"])

    def test_a_command_without_a_title_is_shown_as_itself_shortened(self) -> None:
        command = "python -m pytest -q tests/test_storm_stream.py tests/test_storm_stream_bus.py --maxfail 1 -x"
        line = wording.row_line("command", tool="Bash", title="", target=command + "\n# two lines", status="running")
        self.assertEqual(len(line), wording.COMMAND_SHOWN)
        self.assertTrue(line.startswith("python -m pytest -q tests/") and line.endswith("…"))

    def test_an_mcp_row_is_the_runtimes_own_title(self) -> None:
        self.assertEqual(wording.row_line("mcp", tool="t", title="`create_issue` on `github`", target="anything",
                                          status="ok"), "`create_issue` on `github`")

    def test_a_group_of_only_questions_still_has_a_header(self) -> None:
        t = Turn()
        t.stored("turn.started", origin="person")
        t.tool("q", "ask", "Which port?")
        t.done("q")
        t.stored("turn.finished", status="finished")
        (group,) = present(t.events)
        self.assertEqual(group["header"], "Asked you: Which port?")

    def test_a_live_thinking_block_says_when_it_started(self) -> None:
        live = TurnPresenter(TURN, streams=Streams(thinking_text=True))
        live.durable(StoredEvent(1, TS, "turn.started", {}, turn_id=TURN))
        live.ephemeral(Turn.live(c.ThinkingDelta, block_id="th", delta="x", step_title=None))
        (block,) = live.open_blocks()
        self.assertEqual((block["header"], block["started_at"]), ("Thinking…", TS))   # "Thinking… · 12s" from it


class StoredStepsTests(unittest.TestCase):
    """ch 09 §3: nothing stored is invisible."""

    def test_notices_and_errors_are_rows_that_dont_end_the_answer(self) -> None:
        t = Turn()
        t.stored("turn.started", origin="person")
        t.stored("step.text", block_id="b1", text="Done.")
        t.stored("step.notice", code="api_retry", message="claude: api_retry {\"attempt\": 1}")
        t.stored("step.error", code="overloaded", message="The API is overloaded\nretrying", retryable=True,
                 fatal=False)
        t.stored("turn.finished", status="finished")
        answer, group = present(t.events)
        self.assertEqual((answer["type"], answer["text"]), ("answer", "Done."))
        self.assertEqual([(r["kind"], r["line"], r["status"]) for r in group["rows"]], [
            ("notice", 'claude: api_retry {"attempt": 1}', "ok"), ("error", "The API is overloaded", "error")])
        self.assertEqual(group["header"], "The API is overloaded")

    def test_a_question_marks_its_ask_row_waiting_until_it_is_answered(self) -> None:
        t = Turn()
        live = TurnPresenter(TURN)
        for event in (t.stored("turn.started"), t.tool("q", "ask", "Which port?", tool="AskUserQuestion"),
                      t.stored("step.question", question={"request_id": "r1", "run": None,
                                                          "questions": [{"question": "Which port?"}]})):
            live.durable(event)
        self.assertEqual(live.open_blocks()[0]["rows"][0]["waiting"], True)
        live.durable(t.stored("step.question_done", request_id="r1", answer={"Which port?": "5302"}))
        self.assertEqual(live.open_blocks()[0]["rows"][0]["waiting"], False)

    def test_a_question_without_an_ask_call_gets_its_own_row_ending_with_the_answer(self) -> None:
        t = Turn()
        t.stored("turn.started")
        t.stored("step.question", question={"request_id": "r1", "run": None, "questions": [{"question": "Go on?"}]})
        t.stored("step.question_done", request_id="r1", answer={"Go on?": "yes"})
        t.stored("turn.finished", status="finished")
        (group,) = present(t.events)
        self.assertEqual([(r["call_id"], r["line"], r["status"], r["summary"]) for r in group["rows"]],
                         [("question:r1", "Asked you: Go on?", "ok", "yes")])

    def test_a_step_no_running_turn_presents_is_sent_as_it_is(self) -> None:
        turns = Turns()
        (frame,) = turns.apply(StoredEvent(1, TS, "step.notice", {"code": "credentials_changed", "message": "x"},
                                           session_id="cse_1"))
        self.assertEqual((frame.type, frame.id), ("step.notice", "1"))


class TurnsTests(unittest.TestCase):
    def test_frames_of_one_stored_event_have_their_own_places(self) -> None:
        turns = Turns()
        turns.apply(StoredEvent(1, TS, "turn.started", {}, session_id="cse_1", turn_id=TURN))
        turns.apply(StoredEvent(2, TS, "step.text", {"block_id": "b1", "text": "Hi"}, session_id="cse_1",
                                turn_id=TURN))
        frames = turns.apply(StoredEvent(3, TS, "turn.finished", {}, session_id="cse_1", turn_id=TURN))
        self.assertEqual([(f.type, f.id) for f in frames], [("block.finished", "3:0"), ("turn.finished", "3")])

    def test_a_row_the_presenter_cant_read_is_logged_and_skipped(self) -> None:
        turns = Turns()
        turns.apply(StoredEvent(1, TS, "turn.started", {}, session_id="cse_1", turn_id=TURN))
        with self.assertLogs("services.cowork_agent.storm.stream.turns", "ERROR"):
            self.assertEqual(turns.apply(StoredEvent(2, TS, "step.tool", {"kind": "read"}, turn_id=TURN)), [])
        frames = turns.apply(StoredEvent(3, TS, "turn.finished", {}, session_id="cse_1", turn_id=TURN))
        self.assertEqual([f.type for f in frames], ["turn.finished"])

    def test_a_turn_that_can_no_longer_finish_is_dropped(self) -> None:
        def started(seq: int, turn: str, epoch: int = 1) -> StoredEvent:
            return StoredEvent(seq, TS, "turn.started", {}, session_id="cse_1", turn_id=turn, epoch=epoch)
        for ending in (StoredEvent(2, TS, "session", {"status": "cold"}, session_id="cse_1", epoch=1),
                       StoredEvent(2, TS, "session", {"status": "starting"}, session_id="cse_1", epoch=2),
                       started(2, "turn_2")):
            with self.subTest(ending=ending.type, data=ending.data):
                turns = Turns()
                turns.apply(started(1, TURN))
                turns.apply(StoredEvent(1, TS, "session", {"status": "running"}, session_id="cse_1", epoch=1))
                self.assertIn(TURN, turns.running)
                turns.apply(ending)
                self.assertNotIn(TURN, turns.running)
                self.assertLessEqual(len(turns.running), 1)     # one turn per session, at most


if __name__ == "__main__":
    unittest.main()
