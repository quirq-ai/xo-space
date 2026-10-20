"""Storm's sub-agent runs on the fake claude CLI (design ch 14; ch 18 §2's
proof for PR 14): the run tree from fake events at depth 2 with its
ancestry, progress coalesced to one aggregate a second, a sub-agent's held
call moving its run to ``waiting`` and back, one run stopped and confirmed
only by its end event, a runtime without ``runs.stop``, boot after a crash
(lost runs, the resume note), reconcile with the CLI's files, and transcript
paging with tool outputs cut to 2 KB."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm import stream as storm_stream
from services.cowork_agent.storm.engine import Decision, Unsupported
from services.cowork_agent.storm.engine.ports import RunsAggregate
from services.cowork_agent.storm.engine.ports import Writes
from services.cowork_agent.storm.engine.records import Ids
from services.cowork_agent.storm.runs import operations, records
from services.cowork_agent.storm.runs.operations import TOOL_OUTPUT_MAX_BYTES
from services.cowork_agent.storm.store import Store
from services.cowork_agent.storm.stream import Filter, Point
from tests.fakes import claude
from tests.storm_engine_kit import AGENT, SID, Desk, Kit, ManualClock, message

PUSH = {"command": "git push origin main", "description": "Push"}


def started(*after: dict) -> list[dict]:
    return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]


class NoRunStop(ClaudeRuntime):
    """A runtime whose feature list can't stop one sub-agent."""

    def features(self) -> c.FeatureList:
        listed = super().features()
        return dataclasses.replace(listed, features={**listed.features,
                                                     "runs.stop": c.FeatureSupport(support="unsupported")})


def write_transcripts(home: Path, native_id: str, runs: dict[str, tuple[dict, list[dict]]]) -> Path:
    """claude's files for session ``native_id``: its transcript, and per run
    a ``subagents/agent-<id>.jsonl`` with its ``.meta.json`` sidecar."""
    folder = home / ".claude" / "projects" / "-work"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{native_id}.jsonl").write_text(json.dumps({"type": "user", "uuid": "u0", "message": {
        "role": "user", "content": "hello"}}) + "\n")
    agents = folder / native_id / "subagents"
    agents.mkdir(parents=True, exist_ok=True)
    for run_id, (meta, lines) in runs.items():
        (agents / f"agent-{run_id}.meta.json").write_text(json.dumps(meta))
        (agents / f"agent-{run_id}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    return agents


def turn(n: int, role: str, content: list[dict]) -> dict:
    return {"type": role, "uuid": f"r{n}", "isSidechain": True, "agentId": "disk1", "timestamp": "2026-10-15T09:00:00Z",
            "message": {"role": role, "content": content}}


async def attached(kit: Kit, session_id: str) -> str:
    """The CLI session's own id, once its process is attached."""
    return (await kit.recorder.seen("session", session_id=session_id, reason="attached")).data["native_id"]


class Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    def runs(self, sql: str = "SELECT native_run_id, state, depth, parent_run_id, source FROM runs ORDER BY seq"):
        return self.kit.rows(sql)


class TreeTests(Case):
    async def test_a_tree_at_depth_two_builds_its_rows_and_ancestry(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-a", tool_use_id="toolu_a", description="Check migrations"),
                             claude.task_started(SID, "task-b", tool_use_id="toolu_b", description="List files",
                                                 parent_task_id="task-a", depth=2),
                             claude.task_started(SID, "dream-1", task_type="dream", description="memory"),
                             claude.task_progress(SID, "task-b", tokens=300, tool_uses=3),
                             claude.system(SID, "task_notification", task_id="task-b", status="completed",
                                           summary="x" * 400),
                             claude.system(SID, "task_notification", task_id="task-a", status="failed",
                                           summary="no access"),
                             claude.system(SID, "task_notification", task_id="dream-1", status="completed"),
                             *claude.text_turn(SID, "done"), claude.idle(SID)))
        engine = await kit.boot()
        await engine.write(lambda w: (
            w.execute("INSERT INTO chats (id, kind, project_pid, project_name, created_at) VALUES "
                      "('chat_p1', 'project', 'pid-1', 'demo', ?)", (w.now,)),
            w.execute("INSERT INTO threads (chat_id, ts, title, state, created_at, updated_at) VALUES "
                      "('chat_p1', '1.0', 'Fix the build', 'working', ?, ?)", (w.now, w.now))))
        sid = await engine.create_session(c.SessionSpec(cwd=str(kit.work), permissions=c.PermissionPolicy(mode="ask")),
                                          agent=AGENT, role="thread", preset="thread", chat_id="chat_p1",
                                          thread_ts="1.0")
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)

        a, b = (r[0] for r in kit.rows("SELECT id FROM runs ORDER BY seq"))
        # The internal run is never stored; the child links to its parent's row.
        self.assertEqual(self.runs(), [("task-a", "failed", 1, None, "events"), ("task-b", "finished", 2, a, "events")])
        started_b = kit.recorder.of("run.started", sid)[1]
        self.assertEqual((started_b.run_id, started_b.data["run_id"], started_b.data["parent_run_id"],
                          started_b.data["info"]["depth"], started_b.seq),
                         ("task-b", b, a, 2, kit.rows("SELECT seq FROM runs WHERE id = ?", (b,))[0][0]))
        (tokens, tool_calls, summary), = kit.rows("SELECT tokens, tool_calls, summary FROM runs WHERE id = ?", (b,))
        self.assertEqual((tokens, tool_calls, len(summary)), (300, 3, 280))    # progress kept, summary cut
        self.assertEqual([e.data["run_id"] for e in kit.recorder.of("run.finished", sid)], [b, a])

        run = await operations.get_run(engine, b)
        self.assertEqual(run["ancestry"], {
            "chat": {"id": "chat_p1", "kind": "project", "project_pid": "pid-1", "name": "demo"},
            "thread": {"ts": "1.0", "title": "Fix the build"},
            "session": {"id": sid, "role": "thread", "preset": "thread", "agent": AGENT, "state": "warm"},
            "runs": [{"id": a, "title": "Check migrations", "kind": "subagent", "depth": 1, "state": "failed"}]})
        native_id = (await engine.get_session(sid))["native_id"]
        self.assertEqual((run["can_stop"], run["transcript_ref"]), (False, {"native_id": native_id, "run": "task-b"}))
        self.assertEqual((await operations.get_run(engine, a))["child_counts"],
                         {"total": 1, "running": 0, "waiting": 0})

        listed = await operations.list_runs(engine, sid, tree=True, depth=2)
        (top,) = listed["runs"]
        self.assertEqual((top["id"], [child["id"] for child in top["children"]]), (a, [b]))
        self.assertEqual(listed["counts"], {"running": 0, "waiting": 0, "done": 1, "failed": 1, "stopped": 0,
                                            "lost": 0, "total": 2, "tokens": 300})
        shallow = (await operations.list_runs(engine, sid, tree=True, depth=1))["runs"][0]
        self.assertEqual((shallow["children"], shallow["child_counts"]["total"]), ([], 1))
        flat = await operations.list_runs(engine, sid, states=["finished"])
        self.assertEqual([r["id"] for r in flat["runs"]], [b])
        self.assertEqual([r["id"] for r in (await operations.list_runs(engine, sid, limit=1))["runs"]], [a])

    async def test_a_plain_sessions_ancestry_says_it_has_no_chat(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-a"), *claude.text_turn(SID, "ok"),
                             claude.system(SID, "task_notification", task_id="task-a", status="completed"),
                             claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        finished = await kit.recorder.seen("run.finished", session_id=sid)
        ancestry = (await operations.get_run(engine, finished.data["run_id"]))["ancestry"]
        self.assertEqual((ancestry["chat"], ancestry["thread"], ancestry["runs"], ancestry["session"]["role"]),
                         (None, None, [], "plain"))
        self.assertEqual(await operations.counts(engine, [sid, "cse_none"]), {
            sid: {"running": 0, "waiting": 0, "done": 1, "failed": 0, "stopped": 0, "lost": 0, "total": 1,
                  "tokens": 0},
            "cse_none": {"running": 0, "waiting": 0, "done": 0, "failed": 0, "stopped": 0, "lost": 0, "total": 0,
                         "tokens": 0}})


class ProgressTests(Case):
    async def test_progress_coalesces_to_one_aggregate_a_second_per_session(self) -> None:
        kit = self.kit
        progress = [claude.task_progress(SID, "task-1", tokens=100 * n, tool_uses=n) for n in range(1, 6)]
        kit.scenario(started(claude.task_started(SID, "task-1"), *progress, *claude.text_turn(SID, "launched"),
                             claude.idle(SID), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.wait(lambda: sum(isinstance(e, c.RunProgress) for e in kit.recorder.ephemerals) == 5)
        await kit.recorder.seen("turn.finished", session_id=sid)
        # The start went out at once; five progress reports wait for the second to be up.
        self.assertEqual([(a.running, a.tokens) for a in kit.recorder.aggregates], [(1, 0)])
        listed = await operations.list_runs(engine, sid)            # the live numbers, before the row has them
        self.assertEqual((listed["runs"][0]["tokens"], listed["runs"][0]["tool_calls"], listed["counts"]["tokens"]),
                         (500, 5, 500))
        self.assertTrue(listed["runs"][0]["can_stop"])
        self.assertEqual(kit.rows("SELECT tokens FROM runs"), [(0,)])
        kit.clock.advance(0.5)
        self.assertEqual(len(kit.recorder.aggregates), 1)
        kit.clock.advance(0.5)
        self.assertEqual([(a.session_id, a.running, a.waiting, a.tokens) for a in kit.recorder.aggregates],
                         [(sid, 1, 0, 0), (sid, 1, 0, 500)])
        kit.clock.advance(5)
        self.assertEqual(len(kit.recorder.aggregates), 2)           # nothing changed: nothing sent, nothing polled


class StreamFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_aggregate_is_a_live_frame_for_the_sessions_subscribers(self) -> None:
        bus = storm_stream.listener(ManualClock())
        mine = bus.subscribe(Filter.parse(session="cse_1", kinds="runs"))
        other = bus.subscribe(Filter.parse(session="cse_2", kinds="runs"))
        for subscriber in (mine, other):
            subscriber.go_live(Point(0), {})
        bus.runs(RunsAggregate("cse_1", "chat_1", "1.0", running=2, waiting=1, tokens=900))
        frame = await anext(mine.frames())
        self.assertEqual((frame.type, frame.id, frame.session_id, frame.chat_id, frame.thread_ts, frame.data),
                         ("runs", None, "cse_1", "chat_1", "1.0",
                          {"session": "cse_1", "running": 2, "waiting": 1, "tokens": 900}))
        self.assertEqual(other.backlog, 0)
        bus.close()


class WaitingTests(Case):
    async def test_a_sub_agents_held_call_moves_its_run_to_waiting_and_back(self) -> None:
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        kit.scenario(started(claude.task_started(SID, "task-7", description="Check migrations"), {"sleep": 0.3},
                             claude.can_use_tool("p1", "Bash", PUSH, agent_id="task-7"),
                             claude.permission_answer("p1", "allow"),
                             claude.system(SID, "task_notification", task_id="task-7", status="completed"),
                             *claude.text_turn(SID, "done"), claude.idle(SID)))
        engine = await kit.boot(desk=desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        waiting = await kit.recorder.seen("run.state", session_id=sid, state="waiting")
        (state,), = kit.rows("SELECT state FROM runs")
        self.assertEqual(state, "waiting")
        listed = await operations.list_runs(engine, sid)
        self.assertEqual(listed["counts"]["waiting"], 1)
        # Same transaction as the item: the review opened and the run waits together.
        opened = kit.recorder.of("review.opened", sid)[0]
        self.assertEqual(waiting.seq, opened.seq + 1)
        kit.clock.advance(1)                                        # the aggregate's second is up
        self.assertEqual((kit.recorder.aggregates[-1].running, kit.recorder.aggregates[-1].waiting), (0, 1))

        (item, ask), = desk.opened
        await engine.deliver_decision(item, Decision(allow=True), ask.call, desk.record(item, "allow"))
        back = await kit.recorder.seen("run.state", session_id=sid, state="running")
        self.assertEqual(back.data["run_id"], waiting.data["run_id"])
        finished = await kit.recorder.seen("run.finished", session_id=sid)
        self.assertEqual(finished.data["state"], "finished")


    async def test_a_run_whose_call_reaches_the_host_before_its_start_starts_waiting(self) -> None:
        kit = self.kit
        desk = Desk(loop=asyncio.get_running_loop())
        kit.scenario(started(claude.can_use_tool("p1", "Bash", PUSH, agent_id="task-7"), {"sleep": 0.3},
                             claude.task_started(SID, "task-7", description="Check migrations"), {"sleep": 30}))
        engine = await kit.boot(desk=desk)
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        begun = await kit.recorder.seen("run.started", session_id=sid)
        self.assertEqual(begun.data["state"], "waiting")
        (item, ask), = desk.opened
        await engine.deliver_decision(item, Decision(allow=False), ask.call, desk.record(item, "deny"))
        back = await kit.recorder.seen("run.state", session_id=sid, state="running")
        self.assertEqual(back.data["run_id"], begun.data["run_id"])


class StopTests(Case):
    async def test_stop_one_is_confirmed_only_by_its_end_event(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"), claude.idle(SID),
                             *claude.host_control("stop_task"), {"sleep": 0.5}, *claude.task_killed(SID, "task-1"),
                             claude.system(SID, "background_tasks_changed", tasks=[])))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertTrue(await engine.stop_run(sid, "task-1", by="person"))
        # claude has replied (success, which it says even for an unknown id): the run still runs.
        (state, requested, by), = kit.rows("SELECT state, stop_requested_at IS NOT NULL, stopped_by FROM runs")
        self.assertEqual((state, requested, by), ("running", 1, "person"))
        asked = await kit.recorder.seen("run.state", session_id=sid)
        self.assertEqual((asked.data["state"], asked.data["stopped_by"]), ("running", "person"))
        finished = await kit.recorder.seen("run.finished", session_id=sid)
        self.assertEqual((finished.data["state"], finished.data["status"], finished.data["stopped_by"]),
                         ("stopped", "stopped", "person"))
        self.assertEqual(kit.rows("SELECT state, stopped_by FROM runs"), [("stopped", "person")])
        stop = [line["request"] for run in kit.runs() for line in run["stdin"]
                if line.get("request", {}).get("subtype") == "stop_task"]
        self.assertEqual(stop, [{"subtype": "stop_task", "task_id": "task-1"}])
        self.assertFalse(await engine.stop_run(sid, "task-1", by="person"))      # ended: nothing to stop
        await kit.idle_timer()

    async def test_a_sessions_stop_names_who_stopped_its_runs(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"), claude.idle(SID),
                             *claude.host_control("stop_task"), *claude.task_killed(SID, "task-1")))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertTrue((await engine.stop(sid, by="person")).stopped)
        finished = await kit.recorder.seen("run.finished", session_id=sid)
        self.assertEqual((finished.data["state"], finished.data["stopped_by"]), ("stopped", "person"))

    async def test_a_runtime_without_runs_stop_is_unsupported(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"), claude.idle(SID)))
        engine = await kit.boot_with(NoRunStop(claude_home=kit.home / ".claude"))
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("run.started", session_id=sid)
        with self.assertRaises(Unsupported) as caught:
            await engine.stop_run(sid, "task-1", by="person")
        self.assertEqual((caught.exception.status, caught.exception.feature), (501, "runs.stop"))
        self.assertFalse((await operations.list_runs(engine, sid))["runs"][0]["can_stop"])
        self.assertEqual(kit.rows("SELECT stop_requested_at FROM runs"), [(None,)])


LOST_NOTE = "1 sub-agent stopped when the session restarted; check what they did before relying on it."
RESTART_NOTE = "Your previous turn was interrupted after it started because the server restarted"


class SlowListing(ClaudeRuntime):
    """Its first ``list_runs`` (boot's reconcile) waits for ``gate``."""

    def __init__(self, gate: asyncio.Event, **kw) -> None:
        super().__init__(**kw)
        self.gate = gate
        self.calls = 0

    async def list_runs(self, native_id: str) -> list[c.RunInfo]:
        self.calls += 1
        if self.calls == 1:
            await self.gate.wait()
        return await super().list_runs(native_id)


class ProcessEndTests(Case):
    """ch 14 §5.3: sub-agents that die with their process (a restart, a
    crash) are lost, and the session's next input says how many."""

    async def test_a_graceful_shutdown_mid_turn_loses_the_run_and_the_handed_back_input_says_so(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("run.started", session_id=sid)
        await storm_engine.shutdown()
        self.assertEqual(kit.rows("SELECT state, stopped_by FROM runs"), [("lost", None)])
        self.assertEqual(kit.rows("SELECT runs_lost_untold FROM sessions"), [(1,)])
        (envelope,), = kit.rows("SELECT envelope FROM wakes WHERE state = 'queued'")
        self.assertTrue(envelope.startswith(f"{RESTART_NOTE}; check the state"), envelope)
        kit.scenario(started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        await kit.boot()                                # the handed-back input is re-admitted at once
        text = await self.restart_text(sid)
        self.assertTrue(text.startswith(f"{LOST_NOTE}\n\n{RESTART_NOTE}"), text)       # right before the resume note
        self.assertEqual(kit.rows("SELECT runs_lost_untold FROM sessions"), [(0,)])

    async def restart_text(self, sid: str) -> str:
        """The first input the resumed process got, once nothing waits."""
        kit = self.kit
        await kit.until(lambda: any(len(run["stdin"]) > 1 for run in kit.runs()))
        await kit.until(lambda: kit.rows("SELECT count(*) FROM wakes WHERE state IN ('queued', 'taken')") == [(0,)])
        return kit.runs()[0]["stdin"][1]["message"]["content"][0]["text"]

    async def test_a_restart_during_an_injected_turn_tells_the_next_input(self) -> None:
        """Case C: the turn a helper's result woke has no wakes to hand back."""
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"), claude.idle(SID),
                             *claude.streamed(SID, "m2", [("text", "a helper reported")]), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.started", session_id=sid, origin="injected")
        await storm_engine.shutdown()
        self.assertEqual(kit.rows("SELECT state FROM runs"), [("lost",)])
        self.assertEqual(kit.rows("SELECT count(*) FROM wakes WHERE state = 'queued'"), [(0,)])   # nothing handed back
        kit.scenario(started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        await engine.deliver(sid, message(2, "next"))
        self.assertEqual(await self.restart_text(sid), f"{LOST_NOTE}\n\nnext")
        self.assertEqual(kit.rows("SELECT runs_lost_untold FROM sessions"), [(0,)])

    async def test_a_shutdown_between_turns_tells_the_next_input(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"),
                             claude.idle(SID), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        await storm_engine.shutdown()
        self.assertEqual(kit.rows("SELECT state FROM runs"), [("lost",)])
        self.assertEqual(kit.rows("SELECT runs_lost_untold FROM sessions"), [(1,)])
        kit.scenario(started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        engine = await kit.boot()
        await engine.deliver(sid, message(2, "next"))
        self.assertEqual(await self.restart_text(sid), f"{LOST_NOTE}\n\nnext")
        self.assertEqual(kit.rows("SELECT runs_lost_untold FROM sessions"), [(0,)])

    async def test_a_cli_crash_mid_turn_loses_the_working_run(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), {"sleep": 0.3}, {"exit": 1}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        finished = await kit.recorder.seen("run.finished", session_id=sid)
        self.assertEqual((finished.data["state"], finished.data["status"]), ("lost", "stopped"))
        await kit.recorder.seen("turn.finished", session_id=sid)
        await kit.until(lambda: kit.rows("SELECT runs_lost_untold FROM sessions") == [(1,)])
        (envelope,), = kit.rows("SELECT envelope FROM wakes WHERE state = 'queued'")
        self.assertIn("because the CLI stopped unexpectedly; check the state", envelope)

    async def test_a_stop_the_person_asked_for_is_stopped_even_as_the_process_ends(self) -> None:
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "task-1"), *claude.text_turn(SID, "launched"),
                             claude.idle(SID), *claude.host_control("stop_task"), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertTrue(await engine.stop_run(sid, "task-1", by="person"))
        await storm_engine.shutdown()                   # claude never confirmed; the process ends with it
        self.assertEqual(kit.rows("SELECT state, stopped_by FROM runs"), [("stopped", "person")])
        self.assertEqual(kit.rows("SELECT runs_lost_untold FROM sessions"), [(0,)])

    async def test_boots_reconcile_leaves_a_session_re_admitted_meanwhile_alone(self) -> None:
        """A session recovery fenced and admission re-attached at once: the
        files boot's reconcile reads are the new process's, not lost ones."""
        kit = self.kit
        kit.scenario(started(*claude.text_turn(SID, "ok"), claude.idle(SID)))
        await kit.boot()
        sid = await kit.session()
        await storm_engine.shutdown()
        for sql, params in [
                ("UPDATE sessions SET status = 'running', epoch = 1, native_id = ? WHERE id = ?", (SID, sid)),
                ("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES ('turn_c', ?, 1, "
                 "'person', 'running', '2026-10-15T09:00:00Z')", (sid,)),
                ("INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, turn_id, created_at, "
                 "taken_at) VALUES ('msg:cmsg_1', ?, 'now', 'message', 'do it', '{\"origin\": \"person\"}', "
                 "'taken', 'turn_c', '2026-10-15T09:00:00Z', '2026-10-15T09:00:01Z')", (sid,))]:
            kit.execute(sql, params)
        gate = asyncio.Event()
        engine = await kit.boot_with(SlowListing(gate, claude_home=kit.home / ".claude"))
        await kit.recorder.seen("turn.finished", session_id=sid, status="finished")      # re-admitted: a new epoch
        # The new process's sub-agent's file appears; boot's reconcile reads it only now.
        write_transcripts(kit.home, SID, {"newrun": ({"description": "The new process's", "spawnDepth": 1}, [])})
        gate.set()
        await kit.until(lambda: not any(t.get_name() == "storm-runs-restart" for t in engine._tasks))
        self.assertEqual(kit.rows("SELECT native_run_id, state FROM runs WHERE state = 'lost'"), [])


class RecordTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_run_found_ended_on_disk_works_again_when_its_start_arrives(self) -> None:
        """The idle reconcile can store a run's file as ended while its
        RunStarted is still queued: the start reopens it and its end lands."""
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        store = await Store.open(Path(tmp) / "xo.db")
        self.addAsyncCleanup(store.close)
        ids = Ids("cse_1")
        info = c.RunInfo(run_id="task-r", parent_run_id=None, spawn_call_id=None, kind="subagent", agent_type=None,
                         title="Read", background=True, depth=1)

        def write(fn):
            return store.write(lambda tx: fn(Writes(tx, "2026-10-19T00:00:00Z")))
        await write(lambda w: w.execute(
            "INSERT INTO sessions (id, seq, role, preset, agent, spec, status, created_at, updated_at) "
            "VALUES ('cse_1', 1, 'plain', 'p', 'a', '{}', 'running', 't', 't')"))
        await write(lambda w: records.found(w, ids, [info], epoch=1, native_id="n", state_of=lambda _r: "finished"))
        run_id = await write(lambda w: records.started(w, ids, info, turn_id=None, epoch=1, native_id="n"))
        end = c.RunFinished(session="cse_1", ts="t", seq=1, run="task-r", status="completed", summary="done")
        self.assertEqual(await write(lambda w: records.finished(w, ids, end, epoch=1, progress=None)), "finished")
        rows = await store.read(lambda conn: (
            conn.execute("SELECT id, state, summary FROM runs").fetchall(),
            conn.execute("SELECT type, json_extract(data, '$.state') FROM events ORDER BY seq").fetchall()))
        self.assertEqual(rows[0], [(run_id, "finished", "done")])
        self.assertEqual(rows[1], [("run.started", "finished"), ("run.finished", "finished"),
                                   ("run.started", "running"), ("run.finished", "finished")])


class RestartTests(Case):
    async def test_boot_after_a_crash_loses_working_runs_and_the_next_input_counts_them(self) -> None:
        kit = self.kit
        write_transcripts(kit.home, SID, {"ondisk": ({"agentType": "Explore", "description": "Seen only on disk",
                                                 "toolUseId": "toolu_d", "spawnDepth": 1}, [])})
        kit.scenario(started(*claude.text_turn(SID, "resumed"), claude.idle(SID)))
        await kit.boot()
        sid = await kit.session()
        await storm_engine.shutdown()
        # As the server left it when it died: a turn running, its wake taken, two sub-agents working.
        for sql, params in [
                ("UPDATE sessions SET status = 'running', epoch = 1, native_id = ? WHERE id = ?", (SID, sid)),
                ("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES ('turn_c', ?, 1, "
                 "'person', 'running', '2026-10-15T09:00:00Z')", (sid,)),
                ("INSERT INTO wakes (id, session_id, priority, reason, envelope, meta, state, turn_id, created_at, "
                 "taken_at) VALUES ('msg:cmsg_1', ?, 'now', 'message', 'do it', '{\"origin\": \"person\"}', 'taken', "
                 "'turn_c', '2026-10-15T09:00:00Z', '2026-10-15T09:00:01Z')", (sid,)),
                ("INSERT INTO runs (id, seq, session_id, native_run_id, kind, title, depth, state, started_at, epoch) "
                 "VALUES ('run_1', 1001, ?, 'task-1', 'subagent', 'One', 1, 'running', 't', 1), "
                 "('run_2', 1002, ?, 'task-2', 'subagent', 'Two', 1, 'waiting', 't', 1)", (sid, sid))]:
            kit.execute(sql, params)
        engine = await kit.boot()
        await kit.recorder.seen("turn.finished", session_id=sid, status="finished")
        self.assertEqual(kit.rows("SELECT id, state FROM runs WHERE source = 'events' ORDER BY seq"),
                         [("run_1", "lost"), ("run_2", "lost")])
        (restart,), = kit.rows("SELECT data FROM events WHERE session_id = ? AND type = 'session' AND "
                               "json_extract(data, '$.reason') = 'restart'", (sid,))
        self.assertEqual(json.loads(restart)["runs_lost"], ["run_1", "run_2"])
        (run,) = kit.runs()
        text = run["stdin"][1]["message"]["content"][0]["text"]
        self.assertTrue(text.startswith("2 sub-agents stopped when the session restarted; check what they did before "
                                        f"relying on it.\n\n{RESTART_NOTE}; check the state"), text)
        # Boot's reconcile: a run only the CLI's files show is stored as lost too.
        await kit.recorder.seen("run.started", session_id=sid, source="disk")
        self.assertEqual(kit.rows("SELECT native_run_id, state, title FROM runs WHERE source = 'disk'"),
                         [("ondisk", "lost", "Seen only on disk")])
        self.assertEqual((await operations.list_runs(engine, sid))["counts"]["lost"], 3)


class ReconcileTests(Case):
    async def test_idle_reconcile_stores_a_disk_only_run(self) -> None:
        kit = self.kit
        kit.scenario(started({"sleep": 0.5}, *claude.text_turn(SID, "hi"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        write_transcripts(kit.home, await attached(kit, sid), {
            "disk1": ({"agentType": "Explore", "description": "Found on disk", "toolUseId": "toolu_x",
                       "spawnDepth": 1}, []),
            "disk2": ({"agentType": "Explore", "description": "Its child", "toolUseId": "toolu_y", "spawnDepth": 2,
                       "parentAgentId": "disk1"}, [])})
        await kit.recorder.seen("run.started", session_id=sid, source="disk", count=2)
        parent = kit.rows("SELECT id FROM runs WHERE native_run_id = 'disk1'")[0][0]
        self.assertEqual(self.runs(), [("disk1", "finished", 1, None, "disk"),
                                       ("disk2", "finished", 2, parent, "disk")])
        self.assertEqual(len(kit.recorder.of("run.finished", sid)), 2)
        await kit.idle_timer()

    async def test_a_snapshot_naming_an_unknown_run_stores_it_working(self) -> None:
        kit = self.kit
        kit.scenario(started({"sleep": 0.5}, *claude.text_turn(SID, "hi"),
                             claude.system(SID, "background_tasks_changed", tasks=[{"task_id": "bg1",
                                                                                     "status": "running"}]),
                             {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        write_transcripts(kit.home, await attached(kit, sid), {"bg1": ({"agentType": "Explore",
                                                                         "description": "Started elsewhere",
                                                                         "spawnDepth": 1}, [])})
        found = await kit.recorder.seen("run.started", session_id=sid, source="disk")
        self.assertEqual(found.data["state"], "running")
        self.assertEqual((await engine.get_session(sid))["state"], "running")


    async def test_a_lagging_snapshot_never_revives_a_run_that_ended(self) -> None:
        """Live run 4: a snapshot still listing a run that had ended put it back
        among the live ones; no second end came, and the session stayed
        running for good."""
        kit = self.kit
        kit.scenario(started(claude.task_started(SID, "t1"), *claude.task_killed(SID, "t1"),
                             claude.system(SID, "background_tasks_changed", tasks=[{"task_id": "t1",
                                                                                     "status": "running"}]),
                             *claude.text_turn(SID, "done"), claude.idle(SID), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("run.finished", session_id=sid)
        await kit.until(lambda: kit.rows("SELECT status FROM sessions WHERE id = ?", (sid,)) == [("warm",)])
        view = await engine.get_session(sid)
        self.assertEqual((view["state"], view["live_runs"]), ("warm", 0))

    async def test_a_run_known_only_from_snapshots_ends_when_one_drops_it(self) -> None:
        kit = self.kit
        kit.scenario(started(*claude.text_turn(SID, "hi"),
                             claude.system(SID, "background_tasks_changed", tasks=[{"task_id": "bg1",
                                                                                     "status": "running"}]),
                             {"sleep": 0.5},
                             claude.system(SID, "background_tasks_changed", tasks=[]),
                             claude.idle(SID), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.until(lambda: sid in engine._live)
        live = engine._live[sid]
        await kit.until(lambda: live.facts.runs == {"bg1"})               # learned from the first snapshot
        self.assertEqual(live.status(), "running")
        await kit.until(lambda: not live.facts.runs)                      # gone from the second
        self.assertEqual(live.status(), "warm")


    async def test_a_stored_run_dropped_from_the_snapshots_closes_and_is_never_lost(self) -> None:
        """Its row closes `finished` when a snapshot drops it, so a later
        process end doesn't call it lost and tell the agent so."""
        kit = self.kit
        kit.scenario(started({"sleep": 0.5}, *claude.text_turn(SID, "hi"),
                             claude.system(SID, "background_tasks_changed", tasks=[{"task_id": "bg1",
                                                                                     "status": "running"}]),
                             {"sleep": 1.0},
                             claude.system(SID, "background_tasks_changed", tasks=[]),
                             claude.idle(SID), {"sleep": 30}))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        write_transcripts(kit.home, await attached(kit, sid), {"bg1": ({"agentType": "Explore",
                                                                         "description": "Started elsewhere",
                                                                         "spawnDepth": 1}, [])})
        await kit.recorder.seen("run.started", session_id=sid, source="disk")
        finished = await kit.recorder.seen("run.finished", session_id=sid)
        self.assertEqual((finished.data["state"], finished.data["status"]), ("finished", None))
        self.assertEqual(kit.rows("SELECT state FROM runs"), [("finished",)])
        self.assertEqual(kit.rows("SELECT runs_lost_untold FROM sessions WHERE id = ?", (sid,)), [(0,)])


class TranscriptTests(Case):
    async def test_a_transcript_pages_and_cuts_tool_outputs(self) -> None:
        kit = self.kit
        big = "y" * 3000
        kit.scenario(started({"sleep": 0.5}, *claude.text_turn(SID, "hi"), claude.idle(SID)))
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        agents = write_transcripts(kit.home, await attached(kit, sid), {"disk1": ({"description": "Reader",
                                                                                    "spawnDepth": 1}, [
            turn(1, "user", [{"type": "text", "text": "read it"}]),
            turn(2, "assistant", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a"}}]),
            turn(3, "user", [{"type": "tool_result", "tool_use_id": "t1", "content": big}]),
            turn(4, "user", [{"type": "tool_result", "tool_use_id": "t2",
                              "content": [{"type": "text", "text": "ok"}]}]),
            turn(5, "assistant", [{"type": "text", "text": "done"}])])})
        found = await kit.recorder.seen("run.started", session_id=sid, source="disk")
        run_id = found.data["run_id"]

        first = await operations.read_transcript(engine, run_id, limit=3)
        self.assertEqual([i["id"] for i in first["items"]], ["r1", "r2", "r3"])
        cut = first["items"][2]["message"]["content"][0]
        self.assertEqual((len(cut["content"]), cut["bytes"], cut["truncated"]), (TOOL_OUTPUT_MAX_BYTES, 3000, True))
        rest = await operations.read_transcript(engine, run_id, after=first["next"], limit=3)
        self.assertEqual(([i["id"] for i in rest["items"]], rest["next"]), (["r4", "r5"], None))
        small = rest["items"][0]["message"]["content"][0]
        kept = [{"type": "text", "text": "ok"}]
        self.assertEqual((small["content"], small["bytes"], small["truncated"]), (kept, len(json.dumps(kept)), False))
        with self.assertRaises(operations.RunsRefused) as bad:
            await operations.read_transcript(engine, run_id, after="7")
        self.assertEqual((bad.exception.status, bad.exception.code), (400, "bad_after"))
        (agents / "agent-disk1.jsonl").unlink()
        with self.assertRaises(operations.TranscriptGone) as gone:
            await operations.read_transcript(engine, run_id)
        self.assertEqual((gone.exception.status, gone.exception.summary), (410, None))
        await kit.idle_timer()


if __name__ == "__main__":
    unittest.main()
