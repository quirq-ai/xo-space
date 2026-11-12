"""Coordinator replacement on full context (w5-w7-design W7; the plan's
decision 4 and C).

A coordinator is replaced only when its context is full, when an XO upgrade
changed its role, or when the founder switched its chat's agent. Full is
measured, per runtime: each turn records the context's size at its end, and
the runtime's feature list says where its CLI compacts on its own. A
coordinator within one more input of that point is asked for its note in
its next input; once that input ends it is replaced. The successor starts
only with the next real wake, its handoff (the note, and the facts from the
store) folded into that input: the 11-10 pilot's finding (0 invented
requests in 40 runs once the handoff came with a real message).
"""

from __future__ import annotations

import ast
import json
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.engine import load_runtime, runtime_providers
from services.cowork_agent.storm.chats import live_coordinator, recycle, rows
from services.cowork_agent.storm.health import Health
from services.cowork_agent.storm.records import goals, results, tasks, timers
from tests.fakes import claude
from tests.storm_comms_kit import CommsTestCase
from tests.storm_engine_kit import AGENT
from tests.test_storm_chats_flows import PC, FlowTestCase, next_input, start

#: The window storm's claude coordinators run on; they are replaced at 800k of it.
WINDOW = 1_000_000
#: A note in its six sections, as the outgoing coordinator writes it.
NOTE = {"intent": "Get the login fix shipped by Friday.", "decisions": "Kept /login as a redirect.",
        "results_owed": "Tell the owner the fix is live.", "open_questions": "Whether to keep the old cookie.",
        "founder_requests": "A note in the README.", "next_step": "Wait for the thread's result."}


def features(*points: str, support: str = "native", listed: bool = True) -> c.FeatureList:
    """A runtime's feature list whose coordinators are replaced at 850k of a
    1M window (or at ``points``)."""
    entry = c.FeatureSupport(support=support, values=points or (f"{WINDOW}:850000:measured",))
    return c.FeatureList(agent="fake", cli_version="1", contract_version=c.CONTRACT_VERSION,
                         features={recycle.REPLACE_AT: entry} if listed else {})


def sized(sid: str, n: int, text: str, context: int) -> list[dict]:
    """One turn of a claude coordinator that posts ``text``, its last model
    call holding ``context`` tokens on a 1M window."""
    said = claude.result(sid, text)
    said["emit"]["modelUsage"] = {claude.MODEL: {"contextWindow": WINDOW}}
    return [claude.mcp_call("post_message", {"text": text}),
            {"emit": {"type": "assistant", "session_id": sid, "message": {
                "id": f"msg_{sid}_{n}", "role": "assistant", "content": [{"type": "text", "text": text}],
                "usage": {"input_tokens": context, "output_tokens": 0}}}},
            said, claude.idle(sid)]


def user_inputs(run: dict) -> list[str]:
    """The text of each user message a fake CLI process was sent."""
    return [json.dumps(line) for line in run["stdin"] if line.get("type") == "user"]


class RuleTests(unittest.TestCase):
    """The size rule on a session's recorded turns (a store of its own)."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE turns (id, session_id, origin, usage)")
        self.addCleanup(self.conn.close)

    def turns(self, *contexts: int | None, origin: str = "person", window: int | None = WINDOW) -> None:
        for context in contexts:
            usage = {"input": 9, **({"context_tokens": context} if context is not None else {}),
                     **({"context_window": window} if window is not None else {})}
            self.conn.execute("INSERT INTO turns VALUES (?, 's', ?, ?)", (uuid.uuid4().hex, origin, json.dumps(usage)))

    def state(self, listed: c.FeatureList | None) -> tuple[bool, bool]:
        return recycle.nearly_full(self.conn, "s", listed), recycle.context_full(self.conn, "s", listed)

    def test_the_point_is_the_one_the_runtime_names_for_the_turns_window(self) -> None:
        both = features(f"{WINDOW}:750000:measured", "200000:110000:inferred")
        self.assertEqual(recycle.replace_at(both, WINDOW), 750_000)
        self.assertEqual(recycle.replace_at(both, 200_000), 110_000)
        for window in (475_000, None):                       # a window it doesn't list, a turn that reported none
            with self.subTest(window=window):
                self.assertIsNone(recycle.replace_at(both, window))
        for unknown in (features(listed=False), features(support="unsupported"), None):
            with self.subTest(unknown=unknown):
                self.assertIsNone(recycle.replace_at(unknown, WINDOW))

    def test_reaching_the_point_asks_for_the_note_and_the_next_input_replaces(self) -> None:
        self.turns(100_000, 849_999)
        self.assertEqual(self.state(features()), (False, False))
        self.turns(850_000)                                  # at the point
        self.assertEqual(self.state(features()), (True, False))
        self.turns(860_000)                                  # the input that asked for the note
        self.assertEqual(self.state(features()), (True, True))

    def test_a_turn_its_cli_started_itself_never_counts_as_the_input_that_asked(self) -> None:
        self.turns(100_000, 900_000)
        self.turns(910_000, origin="injected")              # claude's own turn after a worker, same input
        self.assertEqual(self.state(features()), (True, False))

    def test_no_point_or_no_measure_never_replaces_for_size(self) -> None:
        self.turns(100_000, 990_000, 995_000)
        self.assertEqual(self.state(features(listed=False)), (False, False))
        self.assertEqual(self.state(None), (False, False))
        self.assertEqual(self.state(features("200000:110000:inferred")), (False, False))   # not this window
        self.conn.execute("DELETE FROM turns")
        self.turns(None, None, None)                         # a runtime that reports no context size
        self.assertEqual(self.state(features()), (False, False))
        self.conn.execute("DELETE FROM turns")
        self.turns(100_000, 990_000, 995_000, window=None)   # one that reports no window
        self.assertEqual(self.state(features()), (False, False))

    def test_a_smaller_window_is_replaced_at_its_own_point(self) -> None:
        both = features(f"{WINDOW}:750000:measured", "200000:110000:inferred")
        self.turns(30_000, 109_999, window=200_000)
        self.assertEqual(self.state(both), (False, False))
        self.turns(110_000, window=200_000)
        self.assertEqual(self.state(both), (True, False))
        self.turns(120_000, window=200_000)
        self.assertEqual(self.state(both), (True, True))

    def test_a_session_that_started_at_the_point_is_never_replaced_for_size(self) -> None:
        """Its successor would start there too, and be replaced again at once."""
        self.turns(860_000, 870_000, 880_000)
        self.assertEqual(self.state(features()), (False, False))

    def test_only_full_context_an_upgrade_or_an_agent_switch_is_a_reason(self) -> None:
        self.conn.execute("CREATE TABLE sessions (id, created_at)")
        self.conn.execute("INSERT INTO sessions VALUES ('s', '2020-01-01T00:00:00Z')")     # years old
        self.turns(*([20_000] * 60))                         # 60 inputs
        reason = lambda **kw: recycle.reason(self.conn, "s", features=features(), **{
            "fingerprint_changed": False, "agent_changed": False, **kw})
        self.assertIsNone(reason())
        self.assertEqual(reason(fingerprint_changed=True), "upgrade")
        self.assertEqual(reason(agent_changed=True), "agent_switch")
        self.turns(900_000, 910_000)
        self.assertEqual(reason(), "context_full")


class DeclaredTests(unittest.TestCase):
    def test_each_runtime_declares_its_replace_points_as_measured_or_inferred(self) -> None:
        """The 11-12 measurement: claude compacts about 33k under a 1M
        window (22 seen), codex at 244,800 on a 258,400 one (151 seen); no
        compaction was seen on claude's 200k or codex's 475k windows, whose
        points are inferred (the founder, 11-12); agy was never seen
        compacting, so it says unknown."""
        declared = {"claude_code": ("1000000:800000:measured", "200000:110000:inferred"),
                    "codex": ("258400:103360:measured", "475000:190000:inferred"),
                    "antigravity": ()}
        found = {name: load_runtime(name) for name in runtime_providers()}
        self.assertEqual(set(found), set(declared))
        for name, runtime in found.items():
            with self.subTest(name):
                entry = runtime.features().features[recycle.REPLACE_AT]
                self.assertEqual(entry.values, declared[name])
                self.assertEqual(entry.support, "native" if declared[name] else "unsupported")
                for value in entry.values:
                    window, tokens, basis = value.split(":")
                    self.assertEqual(recycle.replace_at(runtime.features(), int(window)), int(tokens))
                    self.assertLess(int(tokens), int(window))
                    self.assertIn(basis, ("measured", "inferred"))


class _Coordinators(CommsTestCase):
    """A project's coordinator with no process (``boot()`` holds every
    session back), its turns written as the engine records them."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)

    async def measured(self, session_id: str, *contexts: int | None, window: int | None = WINDOW) -> None:
        def write(w) -> None:
            for context in contexts:
                usage = {"input": 9, "context_tokens": context, "context_window": window}
                w.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at, ended_at, usage) "
                          "VALUES (?, ?, 0, 'person', 'finished', ?, ?, json(?))",
                          (f"turn_{uuid.uuid4().hex[:12]}", session_id, w.now, w.now, json.dumps(usage)))
        await self.kit.engine.write(write)

    async def upgrade(self, session_id: str) -> None:
        """XO was upgraded: the role's prompt it runs on differs from today's."""
        await self.kit.engine.write(lambda w: w.execute(
            "UPDATE sessions SET spec = json_set(spec, '$.system_prompt.text', "
            "json_extract(spec, '$.system_prompt.text') || ' (as an earlier build wrote it)') WHERE id = ?",
            (session_id,)))

    async def live(self) -> str:
        return await self.kit.engine.store.read(lambda conn: live_coordinator(conn, self.chat["id"]))

    async def handoff(self, session_id: str) -> str | None:
        """What the session's next turn's input starts with."""
        engine = self.kit.engine
        return await recycle.Replacement().note(engine, await engine.meta(session_id), None, "")


class FactsTests(_Coordinators):
    async def test_handoff_keeps_every_unfinished_task_beyond_one_hundred(self) -> None:
        def build(w):
            for i in range(105):
                root = rows.add_message(w, chat_id=self.chat["id"], author_kind="owner", text=f"Task {i}")
                rows.add_thread(w, chat_id=self.chat["id"], ts=root["id"], title=f"Task {i}",
                                session_id=f"cse_task{i}", cwd=str(self.kit.work))
                task, _ = tasks.create(w, chat_id=self.chat["id"], thread_ts=root["id"], title=f"Task {i}",
                                       instructions="Check.", asked_by="founder", by=self.pc, request_key=str(i))
                if i == 0:
                    tasks.move(w, task, state="stopped", actor=self.pc)
            return recycle.facts(w, self.chat["id"], "project-coordinator", since="2099-01-01T00:00:00Z")

        found = await self.kit.engine.write(build)
        self.assertEqual(len(found["tasks"]), 105)
        self.assertIn("stopped", {task["state"] for task in found["tasks"]})

    async def test_every_open_item_is_in_the_successors_handoff(self) -> None:
        chat, pc = self.chat, self.pc
        await self.owner_message(chat, "An old wish from two days ago.")
        self.kit.clock.advance(recycle.FACTS_WINDOW_S + 60)
        asked = await self.owner_message(chat, "Ship the login fix by Friday.")
        root = await self.owner_message(chat, "Fix the login page.")

        def build(w) -> tuple:
            rows.add_thread(w, chat_id=chat["id"], ts=root["id"], title="Fix login", session_id="cse_th",
                            cwd=str(self.kit.work))
            goal, _ = goals.create(w, chat_id=chat["id"], cites=asked["id"], title="Login fixed by Friday",
                                   ends_at="2099-01-01T00:00:00Z", asked_by=pc, request_key="goal-1")
            task, _ = tasks.create(w, chat_id=chat["id"], thread_ts=root["id"], title="Fix login",
                                   instructions="Fix the page.", asked_by="founder", by=pc, request_key="task-1",
                                   goal_id=goal.id)
            task = tasks.report(w, task, text="Found the bad URL.", actor="cse_th")
            result, _ = results.create(w, task, submitted_by="cse_th", agent=AGENT, model="haiku",
                                       summary="Login posts to /api/login.", artifacts=[], cost_usd=None,
                                       request_key="result-1")
            hop = rows.add_hop(w, chat_id=chat["id"], thread_ts=root["id"], kind="ask", from_role="thread",
                               from_session="cse_th", to_role="project-coordinator", to_session=pc,
                               note="Which branch do I start from?")
            seq = w.append("review.opened", {"id": "rv_probe"}, chat_id=chat["id"], thread_ts=root["id"])
            w.execute("INSERT INTO review_items (id, seq, chat_id, thread_ts, session_id, origin, kind, title, state, "
                      "created_at) VALUES ('rv_probe', ?, ?, ?, 'cse_th', 'agent', 'question', 'Keep /login too?', "
                      "'open', ?)", (seq, chat["id"], root["id"], w.now))
            rows.add_message(w, chat_id=chat["id"], thread_ts=root["id"], author_kind="owner", author_id="owner",
                             text="Use the blue button.")
            timer, _ = timers.arm(w, kind="at", chat_id=chat["id"], role="project-coordinator",
                                  note="Check the deploy.", asked_by=pc, by=pc, request_key="timer-1",
                                  due_at="2099-01-01T00:00:00Z")
            return goal, task, result, hop, timer
        goal, task, result, hop, timer = await self.kit.engine.write(build)
        await self.upgrade(pc)
        self.assertEqual(await self.chats.recycle_if_due(chat), "upgrade")
        text = await self.handoff(await self.live())
        for fact in (goal.id, "Login fixed by Friday", task.id, "Found the bad URL.", result.id,
                     "Login posts to /api/login.", hop["id"], "Which branch do I start from?", "rv_probe",
                     "Keep /login too?", root["id"], 'state="needs_reply"', "Ship the login fix by Friday.",
                     "Fix the login page.", "Use the blue button.", timer.id, "Check the deploy."):
            with self.subTest(fact=fact):
                self.assertIn(fact, text)
        self.assertNotIn("An old wish", text)               # the founder's words of the last 24 h only
        self.assertIn('reason="upgrade"', text)
        self.assertIn("left no note", text)


class HandoffNoteTests(_Coordinators):
    async def test_taken_only_while_due_and_its_sections_reach_the_successor(self) -> None:
        token = await self.token(self.pc)
        self.assertIn("isn't nearly full", await self.refused(token, "handoff_note", **NOTE))
        self.assertIsNone(await self.handoff(self.pc))
        await self.measured(self.pc, 100_000, 860_000)      # past the point on claude's 1M window
        self.assertIn(recycle.NEARLY_FULL, await self.handoff(self.pc))
        self.assertEqual(await self.call(token, "handoff_note", **NOTE), {"kept": recycle.NOTE_KEPT})
        self.assertIsNone(await self.chats.recycle_if_due(self.chat))  # not before the input that asked ends
        await self.measured(self.pc, 870_000)
        self.assertEqual(await self.chats.recycle_if_due(self.chat), "context_full")
        new = await self.live()
        self.assertEqual(self.rows("SELECT retired_reason FROM sessions WHERE id = ?", (self.pc,)),
                         [("context_full",)])
        text = await self.handoff(new)
        for section in NOTE.values():
            self.assertIn(section, text)
        self.assertIn('reason="context_full"', text)

    async def test_every_section_is_asked_for(self) -> None:
        await self.measured(self.pc, 100_000, 860_000)
        self.assertIn("next_step", await self.refused(await self.token(self.pc), "handoff_note",
                                                      **{k: v for k, v in NOTE.items() if k != "next_step"}))


class RulesTests(_Coordinators):
    async def test_inputs_age_and_a_runtimes_change_no_longer_replace(self) -> None:
        await self.tool_names(await self.token(self.pc))    # its process lists the runtimes it may start threads on
        await self.measured(self.pc, *([20_000] * 40))
        self.kit.clock.advance(3 * 24 * 3600)
        both = [Health(AGENT, installed=True, logged_in=True, limit="open"),
                Health("other", installed=True, logged_in=True, limit="open")]
        with mock.patch.object(self.chats, "runtime_health", return_value=both):
            self.assertIsNone(await self.chats.recycle_if_due(self.chat))
        self.assertEqual(await self.live(), self.pc)

    async def test_an_upgrade_replaces_saying_why_and_the_successor_waits_for_a_real_wake(self) -> None:
        await self.upgrade(self.pc)
        self.assertEqual(await self.chats.recycle_if_due(self.chat), "upgrade")
        new = await self.live()
        self.assertNotEqual(new, self.pc)
        self.assertEqual(self.rows("SELECT retired_reason, retired_at IS NOT NULL FROM sessions WHERE id = ?",
                                   (self.pc,)), [("upgrade", 1)])
        self.assertEqual(self.rows("SELECT predecessor, incarnation, retired_reason FROM sessions WHERE id = ?",
                                   (new,)), [(self.pc, 2, None)])
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE session_id = ?", (new,)), [(0,)])

    async def test_an_agent_switch_is_due(self) -> None:
        await self.kit.engine.write(lambda w: w.execute(
            "INSERT INTO intelligence (scope, scope_id, agent, set_by, set_at) VALUES ('chat', ?, 'other', "
            "'person', ?)", (self.chat["id"], w.now)))
        self.assertEqual(await self.chats._due(self.chat, self.pc), "agent_switch")

    async def test_the_view_says_whether_a_full_context_replaces_it(self) -> None:
        self.assertIs((await self.kit.engine.get_session(self.pc))["replaced_when_full"], True)
        runtime = self.kit.engine.runtime(AGENT)
        listed = runtime.features()
        unknown = c.FeatureList(agent=listed.agent, cli_version=listed.cli_version,
                                contract_version=listed.contract_version,
                                features={k: v for k, v in listed.features.items() if k != recycle.REPLACE_AT})
        with mock.patch.object(runtime, "features", return_value=unknown):
            self.assertIs((await self.kit.engine.get_session(self.pc))["replaced_when_full"], False)
            await self.measured(self.pc, 100_000, 990_000, 995_000)
            self.assertIsNone(await self.chats.recycle_if_due(self.chat))
        plain = await self.kit.session()
        self.assertIsNone((await self.kit.engine.get_session(plain))["replaced_when_full"])


class PilotTests(FlowTestCase):
    """The 11-10 pilot as a regression test, on the fake claude CLI."""

    async def test_a_full_coordinator_is_replaced_and_its_successor_starts_with_the_next_real_message(self) -> None:
        await self.boot_roles({PC: [*start("pc"), *sized("pc", 1, "One.", 100_000),
                                    next_input(), *sized("pc", 2, "Two.", 830_000),
                                    next_input(), claude.mcp_call("handoff_note", NOTE),
                                    *sized("pc", 3, "Three.", 840_000)]},
                              notes=[recycle.Replacement()])
        chat = (await self.chat_ids())["demo"]
        ended = lambda session_id, n: lambda: self.rows(
            "SELECT count(*) FROM events WHERE type = 'input.ended' AND session_id = ?", (session_id,))[0][0] == n
        words = ("Fix the login page.", "Keep the old URL working.", "How is it going?", "Ship it today.")
        await self.post(chat, words[0])
        old = self.rows("SELECT id FROM sessions WHERE role = 'project-coordinator'")[0][0]
        await self.settle(ended(old, 1))
        await self.post(chat, words[1])                      # its context ends one input short of the point
        await self.settle(ended(old, 2))
        await self.settle(lambda: not self.chats._tasks)
        self.assertEqual(self.rows("SELECT retired_at FROM sessions WHERE id = ?", (old,)), [(None,)])
        await self.post(chat, words[2])                      # asked for its note, it writes one
        await self.settle(lambda: self.rows("SELECT retired_reason FROM sessions WHERE id = ?", (old,))
                          == [("context_full",)])
        [new] = self.rows("SELECT id FROM sessions WHERE role = 'project-coordinator' AND retired_at IS NULL")[0]
        # No turn of its own: nothing is queued for the successor and no process starts for it.
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE session_id = ?", (new,)), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM turns WHERE session_id = ?", (new,)), [(0,)])
        # Each turn recorded the context's size at its end, from the runtime's own report.
        self.assertEqual([json.loads(u)["context_tokens"] for (u,) in self.rows(
            "SELECT usage FROM turns WHERE session_id = ? ORDER BY rowid", (old,))], [100_000, 830_000, 840_000])
        [first_run] = self.kit.runs()
        asked = user_inputs(first_run)
        self.assertEqual([recycle.NEARLY_FULL in said for said in asked], [False, False, True])
        said = await self.post(chat, words[3])
        await self.settle(ended(new, 1))
        self.assertEqual(self.rows("SELECT count(*) FROM turns WHERE session_id = ?", (new,)), [(1,)])
        self.assertEqual(self.rows("SELECT id FROM wakes WHERE session_id = ?", (new,)), [(f"msg:{said['id']}",)])
        [_old_run, new_run] = self.kit.runs()
        [first] = user_inputs(new_run)
        self.assertLess(first.index("<handoff"), first.index(said["id"]))
        for fact in (*NOTE.values(), *words):              # its note, and the founder's words of the last 24 h
            with self.subTest(fact=fact):
                self.assertIn(fact, first)


class WiringTests(unittest.TestCase):
    def test_the_server_starts_every_turn_with_the_replacement_note_first(self) -> None:
        """Production wiring: the engine ``server.py`` boots takes
        ``Replacement`` first among its turn notes, so a successor reads its
        handoff before anything else in its first input."""
        tree = ast.parse((Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8"))
        [boot] = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "boot" and getattr(node.func.value, "id", None) == "storm_review"]
        [notes] = [keyword.value for keyword in boot.keywords if keyword.arg == "notes"]
        self.assertEqual(getattr(notes.elts[0].func, "id", None), recycle.Replacement.__name__)


if __name__ == "__main__":
    unittest.main()
