"""W5: goals, tasks and results, the tools that write them, and the wakes
that go away (record design v2 §2 to §6; w5-w7-design W5).

The records: each change writes its row, its event and its ledger entry in
one transaction; a retried ``request_key`` returns the first row and writes
nothing; every goal and task traces back to the founder's message or the
session that asked; every result names its session, agent, model and cost;
the ledger verifies over all of them. The tools: ``create_task``,
``report``, ``ask``, ``submit_result``, ``set_goal`` and ``read_state``,
their happy paths and refusals. The wakes: a thread's input and the items
it opens no longer wake its coordinator, a failed input still does, and the
coordinator's next real turn starts with what it no longer hears.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from services.cowork_agent.storm import chats as storm_chats
from services.cowork_agent.storm import jobs as storm_jobs
from services.cowork_agent.storm.chats import rows
from services.cowork_agent.storm.chats.digest import SinceLastTurn
from services.cowork_agent.storm.comms import tools, work
from services.cowork_agent.storm.comms.calls import CommsError
from services.cowork_agent.storm.engine.ports import Writes
from services.cowork_agent.storm.records import RecordInvalid, goals, ledger, results, tasks
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.store import Store
from services.timestamps import iso
from tests.fakes import by_prompt, claude, install
from tests.storm_comms_kit import CHOICE, CommsTestCase

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
AT = iso(NOW)
LATER = iso(NOW + timedelta(hours=6))
CHAT = "chat_p0000000000"
TS = "1760518860.000100"
COORD = "cse_coord0000000"
THREAD = "cse_thread000000"


class _Store(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = await Store.open(self.dir / "xo.db")
        self.addAsyncCleanup(self.store.close)
        self.batches: list[list[Any]] = []
        self.store.on_commit(self.batches.append)

        def chat_and_thread(tx) -> None:
            tx.execute("INSERT INTO chats (id, kind, project_pid, project_name, created_at) "
                       "VALUES (?, 'project', 'pid-1', 'demo', ?)", (CHAT, AT))
            tx.execute("INSERT INTO threads (chat_id, ts, title, created_at, updated_at) "
                       "VALUES (?, ?, 'Fix login', ?, ?)", (CHAT, TS, AT, AT))
        await self.store.write(chat_and_thread)

    async def write(self, fn: Callable[[Writes], Any]) -> Any:
        return await self.store.write(lambda tx: fn(Writes(tx, AT)))

    async def rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        return await self.store.read(lambda conn: conn.execute(sql, params).fetchall())

    async def tamper(self, *sql: str) -> None:
        """Change the store past the records modules, as a confused agent would."""
        def run(tx) -> None:
            for statement in sql:
                tx.execute(statement)
        await self.store.write(run)

    async def goal(self, key: str = "k-goal") -> tuple[goals.Goal, bool]:
        return await self.write(lambda w: goals.create(w, chat_id=CHAT, cites="cmsg_1", title="Ship the login fix",
                                                       ends_at=LATER, asked_by=COORD, request_key=key))

    async def task(self, key: str = "k-task", **kw: Any) -> tuple[tasks.Task, bool]:
        return await self.write(lambda w: tasks.create(w, chat_id=CHAT, thread_ts=TS, title="Fix login",
                                                       instructions="The form posts to the wrong URL.",
                                                       asked_by=kw.pop("asked_by", COORD), by=COORD,
                                                       request_key=key, **kw))

    async def result(self, task: tasks.Task, key: str = "k-result") -> tuple[results.Result, bool]:
        return await self.write(lambda w: results.create(
            w, task, submitted_by=THREAD, agent="agent_a", model="model-1", summary="Fixed the URL.",
            artifacts=[{"kind": "commit", "ref": "HEAD", "sha": "a" * 40}], cost_usd=0.42, request_key=key))

    def events(self, type_: str) -> list[Any]:
        return [e for batch in self.batches for e in batch if e.type == type_]


class RecordTests(_Store):
    async def test_each_record_traces_to_the_founders_message_or_the_session_that_asked(self) -> None:
        """Invariant 1."""
        goal, _made = await self.goal()
        self.assertEqual((goal.cites, goal.asked_by, goal.state), ("cmsg_1", COORD, "active"))
        task, _made = await self.task(goal_id=goal.id, asked_by=ledger.FOUNDER)
        self.assertEqual((task.asked_by, task.goal_id, task.thread_ts), (ledger.FOUNDER, goal.id, TS))
        self.assertEqual(await self.rows("SELECT actor FROM ledger WHERE record = ?", (f"task:{task.id}",)),
                         [(COORD,)])                            # who recorded it, beside who asked
        for asker in ("someone", "", "coordinator"):
            with self.subTest(asker), self.assertRaises(RecordInvalid):
                await self.write(lambda w: tasks.create(w, chat_id=CHAT, thread_ts=TS, title="t", instructions="i",
                                                        asked_by=asker, by=COORD, request_key=f"k-{asker}"))
        with self.assertRaises(sqlite3.IntegrityError):         # the store holds the rule too
            await self.tamper("UPDATE tasks SET asked_by = 'someone'")

    async def test_the_same_request_key_gives_the_same_row_and_no_second_event(self) -> None:
        """Invariant 7."""
        first, made = await self.goal()
        again, made_again = await self.goal()
        self.assertEqual((made, made_again, again), (True, False, first))
        task, _ = await self.task()
        self.assertEqual(await self.task(), (task, False))
        result, _ = await self.result(task)
        self.assertEqual(await self.result(task), (result, False))
        self.assertEqual([len(self.events(t)) for t in ("goal", "task", "result")], [1, 1, 1])
        self.assertEqual(len(await self.rows("SELECT seq FROM ledger")), 3)

    async def test_a_record_its_event_and_its_ledger_entry_commit_together(self) -> None:
        """Invariant 8: one commit holds all three, and a transaction that
        fails after the record keeps none of them."""
        await self.goal()
        [batch] = [b for b in self.batches if any(e.type == "goal" for e in b)]
        self.assertEqual([e.type for e in batch], ["goal"])
        self.assertEqual(await self.rows("SELECT action, record FROM ledger"),
                         [("create", f"goal:{(await self.rows('SELECT id FROM goals'))[0][0]}")])

        def then_fail(w: Writes) -> None:
            tasks.create(w, chat_id=CHAT, thread_ts=TS, title="t", instructions="i", asked_by=COORD, by=COORD,
                         request_key="k-fails")
            raise RuntimeError("after the record")
        with self.assertRaises(RuntimeError):
            await self.write(then_fail)
        self.assertEqual(await self.rows("SELECT count(*) FROM tasks"), [(0,)])
        self.assertEqual(await self.rows("SELECT count(*) FROM events WHERE type = 'task'"), [(0,)])
        self.assertEqual(await self.rows("SELECT count(*) FROM ledger"), [(1,)])

    async def test_the_ledger_verifies_over_every_record_and_finds_an_edit_outside_it(self) -> None:
        """Invariant 9, for goals, tasks and results."""
        goal, _ = await self.goal()
        task, _ = await self.task(goal_id=goal.id)
        task = await self.write(lambda w: tasks.report(w, task, text="Half way.", actor=THREAD))
        result, _ = await self.result(task)
        await self.write(lambda w: tasks.move(w, task, state="done", actor=THREAD))
        await self.write(lambda w: goals.move(w, goal, state="done", reason="shipped", actor=COORD))
        self.assertEqual(await self.store.read(ledger.verify), [])
        self.assertEqual([r for (r,) in await self.rows("SELECT action FROM ledger ORDER BY seq")],
                         ["create", "create", "report", "create", "move", "move"])
        cases = (("UPDATE tasks SET last_report = 'All done.'", f"task:{task.id}", "changed"),
                 ("UPDATE goals SET ends_at = '2027-01-01T00:00:00Z'", f"goal:{goal.id}", "changed"),
                 ("DELETE FROM results", f"result:{result.id}", "gone"))
        for tamper, record, words in cases:
            with self.subTest(record):
                await self.tamper(tamper)
                found = [f for f in await self.store.read(ledger.verify) if f.record == record]
                # A task's row edited outside its module also no longer agrees with its report history.
                self.assertEqual(len(found), 2 if record.startswith("task:") else 1)
                self.assertIn(words, found[0].problem)

    async def test_every_report_is_kept_in_order_and_the_newest_is_the_tasks(self) -> None:
        task, _ = await self.task()
        for text in ("Started.", "Half way.", "Half way.", "Done, pushed to main."):
            task = await self.write(lambda w, text=text: tasks.report(w, task, text=text, actor=THREAD))
        self.assertEqual(task.last_report, "Done, pushed to main.")
        kept = await self.store.read(lambda c: tasks.reports(c, task.id, limit=10))
        self.assertEqual([(r.text, r.by) for r in kept],
                         [("Done, pushed to main.", THREAD), ("Half way.", THREAD), ("Started.", THREAD)],
                         "newest first; a report the same as the one before isn't kept twice")
        older = await self.store.read(lambda c: tasks.reports(c, task.id, limit=10, before=kept[0].seq))
        self.assertEqual([r.text for r in older], ["Half way.", "Started."])
        self.assertEqual(await self.store.read(ledger.verify), [])
        for tamper in ("UPDATE task_reports SET text = 'x'", "DELETE FROM task_reports"):
            with self.subTest(tamper), self.assertRaises(Exception):
                await self.tamper(tamper)
        await self.tamper("INSERT INTO task_reports (task_id, text, by, at) VALUES "
                          f"('{task.id}', 'All good.', '{THREAD}', '2026-11-13T00:00:00Z')")
        found = [f for f in await self.store.read(ledger.verify) if f.record == f"task:{task.id}"]
        self.assertEqual(len(found), 1)
        self.assertIn("newest report", found[0].problem)

    async def test_every_result_names_its_session_agent_model_and_cost(self) -> None:
        """Invariant 10."""
        task, _ = await self.task()
        result, _ = await self.result(task)
        row = (await self.rows("SELECT submitted_by, agent, model, cost_usd, artifacts FROM results"))[0]
        self.assertEqual(row[:4], (THREAD, "agent_a", "model-1", 0.42))
        self.assertEqual(json.loads(row[4]), [{"kind": "commit", "ref": "HEAD", "sha": "a" * 40}])
        for missing in ({"submitted_by": "nobody"}, {"agent": ""}, {"model": ""}):
            with self.subTest(missing), self.assertRaises(RecordInvalid):
                await self.write(lambda w: results.create(w, task, **{
                    "submitted_by": THREAD, "agent": "agent_a", "model": "m", "summary": "s", "artifacts": [],
                    "cost_usd": None, "request_key": f"k-{missing}", **missing}))

    async def test_a_record_is_never_edited_in_place_only_moved_forward(self) -> None:
        """Rule 8: a final goal stays final; a task moves by state, and a
        move to the state it has writes nothing."""
        goal, _ = await self.goal()
        done = await self.write(lambda w: goals.move(w, goal, state="done", reason=None, actor=COORD))
        with self.assertRaises(RecordInvalid):
            await self.write(lambda w: goals.move(w, done, state="active", reason=None, actor=COORD))
        task, _ = await self.task()
        entries = len(await self.rows("SELECT seq FROM ledger"))
        self.assertIs(await self.write(lambda w: tasks.move(w, task, state="open", actor=COORD)), task)
        self.assertEqual(len(await self.rows("SELECT seq FROM ledger")), entries)
        with self.assertRaises(sqlite3.IntegrityError):          # one task per thread
            await self.task(key="k-second")


# ── the tools ────────────────────────────────────────────────────────────────

class _Project(CommsTestCase):
    """A project with its coordinator, the owner's message and a task the
    coordinator started on it; no session's process runs (the test calls
    the tools itself)."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        self.root = await self.owner_message(self.chat, "Fix the login page")
        self.started = await self.call(self.pc_token, "create_task", message_id=self.root["id"], title="Fix login",
                                       instructions="The form posts to the wrong URL.", **CHOICE)
        self.th = self.started["session_id"]
        self.th_token = await self.token(self.th)
        self.folder = Path(self.rows("SELECT cwd FROM threads WHERE ts = ?", (self.root["id"],))[0][0])

    def coordinator_wakes(self) -> list[str]:
        return [w[0] for w in self.wakes(self.pc)]

    async def findings(self) -> list[Any]:
        return await self.kit.engine.store.read(ledger.verify)


class CreateTaskTests(_Project):
    async def test_the_task_and_its_thread_are_made_in_one_transaction(self) -> None:
        task_id = self.started["task_id"]
        [row] = self.rows("SELECT chat_id, thread_ts, title, instructions, state, asked_by, goal_id FROM tasks")
        self.assertEqual(row, (self.chat["id"], self.root["id"], "Fix login", "The form posts to the wrong URL.",
                               "open", "founder", None))         # on the owner's own message: theirs, citing it
        [batch] = self.commits.with_type("task")
        self.assertLessEqual({"task", "comms.call", "hop", "thread"}, {e.type for e in batch})
        self.assertEqual(self.rows("SELECT actor, action, record FROM ledger"),
                         [(self.pc, "create", f"task:{task_id}")])
        self.assertEqual(await self.findings(), [])
        self.assertEqual(json.loads(self.rows("SELECT data FROM events WHERE type = 'comms.call' AND "
                                              "json_extract(data, '$.tool') = 'create_task'")[0][0])["task_id"],
                         task_id)

    async def test_a_retried_call_returns_the_first_task_and_starts_nothing(self) -> None:
        again = await self.call(self.pc_token, "create_task", message_id=self.root["id"], title="Fix login",
                                instructions="The form posts to the wrong URL.", **CHOICE)
        self.assertEqual((again["task_id"], again["session_id"], again["repeated"]),
                         (self.started["task_id"], self.th, True))
        self.assertEqual(self.rows("SELECT count(*) FROM sessions WHERE role = 'thread'"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'task'"), [(1,)])

    async def test_any_timeline_message_anchors_a_task_asked_by_the_coordinator_unless_the_founders(self) -> None:
        """Invariant 1: a task traces to the founder's citation or to the
        session that asked, never to nothing."""
        said = await self.call(self.pc_token, "post_message", text="I'll look into the footer too.")
        own = await self.call(self.pc_token, "create_task", message_id=said["message_id"], title="Footer",
                              instructions="x", **CHOICE)
        copy = await self.kit.engine.write(lambda w: rows.add_message(
            w, chat_id=self.chat["id"], author_kind="owner", author_id="owner", text="Add dark mode",
            extra={"forwarded_from": {"chat_id": "chat_w", "message_id": "cmsg_w1", "via": ["workspace"]}}))
        forwarded = await self.call(self.pc_token, "create_task", message_id=copy["id"], title="Dark mode",
                                    instructions="x", **CHOICE)
        self.assertEqual(sorted(self.rows("SELECT id, asked_by FROM tasks")),
                         sorted([(self.started["task_id"], "founder"), (own["task_id"], self.pc),
                                 (forwarded["task_id"], self.pc)]))
        elsewhere = await self.owner_message(await self.project("other"), "Not yours")
        self.assertIn("not a message on this chat's timeline", await self.refused(
            self.pc_token, "create_task", message_id=elsewhere["id"], title="t", instructions="x", **CHOICE))
        self.assertIn("already has a thread", await self.refused(
            self.pc_token, "create_task", message_id=self.root["id"], title="another", instructions="x", **CHOICE))
        later = await self.owner_message(self.chat, "And the footer")
        self.assertIn("no goal", await self.refused(self.pc_token, "create_task", message_id=later["id"], title="t",
                                                    instructions="x", goal_id="goal_nope", **CHOICE))
        self.assertEqual(self.rows("SELECT count(*) FROM tasks"), [(3,)])

    async def test_it_still_chooses_the_threads_model_and_effort(self) -> None:
        listed = (await self.rpc(self.pc_token, "tools/list")).json()["result"]["tools"]
        [create] = [tool for tool in listed if tool["name"] == "create_task"]
        self.assertIn("model", create["inputSchema"]["required"])
        self.assertIn("goal_id", create["inputSchema"]["properties"])
        self.assertEqual(self.rows("SELECT model, effort FROM intelligence WHERE scope = 'thread'"),
                         [tuple(CHOICE.values())])


class GoalTests(_Project):
    async def test_a_goal_cites_the_founders_message_and_a_task_links_to_it(self) -> None:
        set_ = await self.call(self.pc_token, "set_goal", message_id=self.root["id"], title="Login works",
                               ends_at=tomorrow())
        [row] = self.rows("SELECT id, cites, asked_by, state FROM goals")
        self.assertEqual(row, (set_["goal_id"], self.root["id"], self.pc, "active"))
        later = await self.owner_message(self.chat, "And the footer")
        linked = await self.call(self.pc_token, "create_task", message_id=later["id"], title="Footer",
                                 instructions="x", goal_id=set_["goal_id"], **CHOICE)
        self.assertEqual(self.rows("SELECT goal_id FROM tasks WHERE id = ?", (linked["task_id"],)),
                         [(set_["goal_id"],)])
        closed = await self.call(self.pc_token, "set_goal", goal_id=set_["goal_id"], state="done", reason="shipped")
        self.assertEqual((closed["state"], closed["state_reason"]), ("done", "shipped"))
        self.assertIn("already done", await self.refused(self.pc_token, "set_goal", goal_id=set_["goal_id"],
                                                         state="failed"))
        self.assertEqual(await self.findings(), [])

    async def test_a_retried_goal_is_the_same_goal(self) -> None:
        end = tomorrow()
        first = await self.call(self.pc_token, "set_goal", message_id=self.root["id"], title="Login works",
                                ends_at=end)
        again = await self.call(self.pc_token, "set_goal", message_id=self.root["id"], title="Login works",
                                ends_at=end)
        self.assertEqual(again["goal_id"], first["goal_id"])
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'goal'"), [(1,)])

    async def test_refusals(self) -> None:
        said = await self.call(self.pc_token, "post_message", text="noted")
        self.assertIn("owner's word", await self.refused(self.pc_token, "set_goal", message_id=said["message_id"],
                                                             title="t", ends_at=tomorrow()))
        self.assertIn("in the future", await self.refused(self.pc_token, "set_goal", message_id=self.root["id"],
                                                          title="t", ends_at="2020-01-01T00:00:00Z"))
        self.assertIn("ISO-8601", await self.refused(self.pc_token, "set_goal", message_id=self.root["id"],
                                                     title="t", ends_at="tomorrow"))
        self.assertIn("close a goal", await self.refused(self.pc_token, "set_goal", message_id=self.root["id"],
                                                         title="t", ends_at=tomorrow(), state="done"))
        self.assertIn("no goal", await self.refused(self.pc_token, "set_goal", goal_id="goal_nope", state="done"))
        self.assertIn("is not a tool", await self.refused(self.th_token, "set_goal", message_id=self.root["id"],
                                                          title="t", ends_at=tomorrow()))
        self.assertEqual(self.rows("SELECT count(*) FROM goals"), [(0,)])


def tomorrow() -> str:
    """A goal's end, a day from the wall clock (the server checks it against its own)."""
    return iso(datetime.now(timezone.utc) + timedelta(days=1))


class ReportAndResultTests(_Project):
    async def test_a_report_lands_on_the_task_and_wakes_no_one(self) -> None:
        before = self.coordinator_wakes()
        reported = await self.call(self.th_token, "report", text="Found the bad URL; fixing it.")
        self.assertIn("wakes no one", reported["note"])
        self.assertEqual(self.rows("SELECT last_report, reported_at IS NOT NULL FROM tasks"),
                         [("Found the bad URL; fixing it.", 1)])
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'task.report'"), [(1,)])
        self.assertEqual(self.coordinator_wakes(), before)
        self.assertEqual(self.rows("SELECT count(*) FROM hops WHERE from_session = ?", (self.th,)), [(0,)])
        self.assertIn("is not a tool", await self.refused(self.pc_token, "report", text="x"))
        self.assertEqual(await self.findings(), [])

    async def test_a_result_is_recorded_as_reported_with_the_servers_hashes(self) -> None:
        content = b"The form now posts to /api/login.\n"
        (self.folder / "notes.md").write_bytes(content)
        await self.kit.engine.write(lambda w: w.execute(
            "INSERT INTO turns (id, session_id, epoch, origin, status, started_at, cost_usd) VALUES "
            "('turn_c1', ?, 1, 'wake', 'finished', ?, 0.25), ('turn_c2', ?, 1, 'wake', 'finished', ?, 0.5)",
            (self.th, w.now, self.th, w.now)))
        before = self.coordinator_wakes()
        done = await self.call(self.th_token, "submit_result", summary="Fixed the login URL.",
                               artifacts=[{"path": "notes.md"}])
        self.assertEqual(done["task_state"], "done")
        [artifact] = done["artifacts"]
        self.assertEqual(artifact, {"kind": "file", "path": "notes.md", "bytes": len(content),
                                    "sha256": hashlib.sha256(content).hexdigest()})
        [row] = self.rows("SELECT submitted_by, agent, model, cost_usd, task_id FROM results")
        self.assertEqual(row[:2], (self.th, self.rows("SELECT agent FROM sessions WHERE id = ?", (self.th,))[0][0]))
        self.assertTrue(row[2])
        self.assertEqual((row[3], row[4]), (0.75, self.started["task_id"]))
        self.assertEqual(self.rows("SELECT state FROM tasks"), [("done",)])
        self.assertEqual(self.coordinator_wakes(), before)
        again = await self.call(self.th_token, "submit_result", summary="Fixed the login URL.",
                                artifacts=[{"path": "notes.md"}])
        self.assertEqual(again["result_id"], done["result_id"])
        (self.folder / "notes.md").write_text("Changed after review.\n")
        changed = await self.call(self.th_token, "submit_result", summary="Fixed the login URL.",
                                  artifacts=[{"path": "notes.md"}])
        self.assertNotEqual(changed["result_id"], done["result_id"])
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'result'"), [(2,)])
        self.assertEqual(await self.findings(), [])

    async def test_refusals(self) -> None:
        outside = Path(self.kit.root) / "secret.txt"
        outside.write_text("x")
        for artifacts, words in (([{"path": str(outside)}], "isn't inside your working folder"),
                                 ([{"path": "missing.md"}], "no file"),
                                 ([{"path": "a", "commit": "HEAD"}], "Each artifact"),
                                 ([{"commit": "--upload-pack=x"}], "isn't a ref"),
                                 ([{"commit": "HEAD"}], "isn't a commit")):       # the project isn't a repository
            with self.subTest(words):
                self.assertIn(words, await self.refused(self.th_token, "submit_result", summary="s",
                                                        artifacts=artifacts))
        self.assertEqual(self.rows("SELECT count(*) FROM results"), [(0,)])
        self.assertIn("is not a tool", await self.refused(self.pc_token, "submit_result", summary="s"))

    async def test_a_commit_is_hashed_by_git(self) -> None:
        repo = Path(self.kit.root) / "repo"
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x"}
        for argv in (["git", "init", "-q"], ["git", "commit", "-q", "--allow-empty", "-m", "one"]):
            subprocess.run(argv, cwd=repo, env=env, check=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
        self.assertEqual(await work._commit(str(repo), "HEAD"), {"kind": "commit", "ref": "HEAD", "sha": sha})
        with self.assertRaises(CommsError):
            await work._commit(str(repo), "no-such-branch")

    async def test_a_thread_without_a_task_gets_one_at_its_first_report(self) -> None:
        """A thread from before the upgrade: its task is made from its brief,
        asked as create_task would have it: the founder's on their message,
        else the coordinator's that sent the brief."""
        said = await self.call(self.pc_token, "post_message", text="I'll tidy the footer.")
        other = await self.call(self.pc_token, "create_task", message_id=said["message_id"], title="Footer",
                                instructions="Tidy it.", **CHOICE)
        await self.kit.engine.write(lambda w: w.execute("DELETE FROM tasks"))
        await self.call(self.th_token, "report", text="Still on it.")
        await self.call(await self.token(other["session_id"]), "report", text="Tidied.")
        self.assertEqual(sorted(self.rows("SELECT instructions, asked_by, last_report FROM tasks")),
                         sorted([("The form posts to the wrong URL.", "founder", "Still on it."),
                                 ("Tidy it.", self.pc, "Tidied.")]))


class AskTests(_Project):
    async def test_an_ask_to_the_founder_opens_an_item_and_wakes_no_coordinator(self) -> None:
        before = self.coordinator_wakes()
        asked = await self.call(self.th_token, "ask", to="founder", kind="approval", title="Force-push release",
                                why="History needs fixing", detail={"command": "git push --force origin release"},
                                blocking=True)
        self.assertEqual(self.rows("SELECT kind, risk, blocking, thread_ts FROM review_items WHERE id = ?",
                                   (asked["review_id"],)), [("approval", "destructive", 1, self.root["id"])])
        self.assertEqual(self.thread_rows(), [("needs_approval",)])
        self.assertEqual(self.coordinator_wakes(), before)
        self.assertEqual(self.rows("SELECT count(*) FROM hops WHERE kind = 'ask'"), [(0,)])
        call = json.loads(self.rows("SELECT data FROM events WHERE type = 'comms.call' AND "
                                    "json_extract(data, '$.tool') = 'ask'")[0][0])
        self.assertEqual((call["to"], call["blocking"]), ("founder", True))

    async def test_an_ask_to_the_coordinator_wakes_it_and_its_answer_closes_the_ask(self) -> None:
        asked = await self.call(self.th_token, "ask", to="coordinator", question="Which URL is right?")
        self.assertEqual(self.rows("SELECT kind, from_session, to_session FROM hops WHERE id = ?", (asked["hop_id"],)),
                         [("ask", self.th, self.pc)])
        self.assertIn(f"deleg:{asked['hop_id']}", self.coordinator_wakes())
        answered = await self.call(self.pc_token, "message_thread", thread_id=self.root["id"], note="/api/login")
        self.assertEqual(self.rows("SELECT kind, cause FROM hops WHERE id = ?", (answered["hop_id"],)),
                         [("answer", asked["hop_id"])])

    async def test_a_project_coordinator_asks_the_workspace(self) -> None:
        asked = await self.call(self.pc_token, "ask", to="workspace", question="Who owns auth?",
                                on_behalf_of=self.root["id"])
        ws = self.rows("SELECT id FROM sessions WHERE role = 'workspace-coordinator'")[0][0]
        self.assertEqual(self.rows("SELECT kind, to_session FROM hops WHERE id = ?", (asked["hop_id"],)),
                         [("ask", ws)])
        self.assertEqual(len(self.wakes(ws)), 1)

    async def test_each_role_reaches_only_whom_it_may_and_each_ask_takes_its_own_fields(self) -> None:
        self.assertIn("to is one of founder, coordinator", await self.refused(
            self.th_token, "ask", to="workspace", question="q"))
        self.assertIn("to is one of founder, workspace", await self.refused(
            self.pc_token, "ask", to="coordinator", question="q"))
        self.assertIn("not question", await self.refused(self.th_token, "ask", to="founder", kind="question",
                                                          title="t", why="w", question="q"))
        self.assertIn("not kind", await self.refused(self.th_token, "ask", to="coordinator", question="q",
                                                      kind="question"))
        self.assertIn("question is required", await self.refused(self.th_token, "ask", to="coordinator"))
        listed = (await self.rpc(self.th_token, "tools/list")).json()["result"]["tools"]
        [ask] = [tool for tool in listed if tool["name"] == "ask"]
        self.assertEqual(ask["inputSchema"]["properties"]["to"], {"enum": ["founder", "coordinator"]})


class ReadStateTests(_Project):
    async def test_each_role_reads_its_own_records_and_the_advisory(self) -> None:
        await self.call(self.th_token, "report", text="Half way.")
        asked = await self.call(self.th_token, "ask", to="founder", kind="question", title="SES or Postmark?",
                                why="email")
        mine = await self.call(self.th_token, "read_state")
        self.assertEqual(set(mine["advisory"]), {"version", "as_of", "served_at", "accounts", "machine", "sessions"})
        [task] = mine["tasks"]
        self.assertEqual((task["task_id"], task["last_report"], task["results"]),
                         (self.started["task_id"], "Half way.", 0))
        self.assertEqual([q["review_id"] for q in mine["open_questions"]], [asked["review_id"]])
        self.assertNotIn("open_items", mine)
        goal = await self.call(self.pc_token, "set_goal", message_id=self.root["id"], title="Login works",
                               ends_at=tomorrow())
        theirs = await self.call(self.pc_token, "read_state")
        self.assertEqual([t["task_id"] for t in theirs["tasks"]], [self.started["task_id"]])
        self.assertEqual([g["goal_id"] for g in theirs["goals"]], [goal["goal_id"]])
        self.assertEqual([i["review_id"] for i in theirs["open_items"]], [asked["review_id"]])
        self.assertEqual(theirs["open_questions"], [])


class ReplacedToolTests(_Project):
    async def test_every_role_lists_the_new_tools_and_none_of_the_replaced(self) -> None:
        for token, role in ((self.pc_token, "project-coordinator"), (self.th_token, "thread")):
            with self.subTest(role):
                names = set(await self.tool_names(token))
                self.assertFalse(names & set(tools.REPLACED))
                self.assertIn("ask", names)
                self.assertIn("read_state", names)
        self.assertFalse(set(tools.REPLACED) & set(tools.TOOLS))

    async def test_a_session_from_before_the_upgrade_hears_where_a_replaced_tool_went(self) -> None:
        later = await self.owner_message(self.chat, "And the footer")
        self.assertEqual(await self.refused(self.pc_token, "start_thread_session", message_id=later["id"],
                                            title="Footer", instructions="x", **CHOICE),
                         "'start_thread_session' was replaced: use create_task, with the same arguments.")
        self.assertIn('ask with to "coordinator"', await self.refused(self.th_token, "message_coordinator", text="q"))
        self.assertEqual(self.rows("SELECT json_extract(data, '$.error') FROM events WHERE type = 'comms.call' "
                                   "AND json_extract(data, '$.ok') = 0"), [("unknown_tool",), ("unknown_tool",)])
        self.assertEqual(self.rows("SELECT count(*) FROM tasks"), [(1,)])

    def test_a_replaced_tool_is_refused_naming_its_replacement(self) -> None:
        self.assertEqual(tools.unknown_tool("start_thread_session", "project-coordinator"),
                         "'start_thread_session' was replaced: use create_task, with the same arguments.")
        self.assertIn('ask with to "founder"', tools.unknown_tool("ask_owner", "thread"))
        self.assertIn("report for progress", tools.unknown_tool("message_coordinator", "thread"))
        self.assertEqual(tools.unknown_tool("start_thread_session", "thread"),
                         "'start_thread_session' is not a tool for a thread.")


# ── the wakes that go away ───────────────────────────────────────────────────

class WakeTests(_Project):
    async def input(self, *, status: str = "finished", stopped_by: str | None = None,
                    during: Callable[[], Any] | None = None) -> None:
        """One input of the thread, as the engine records it, judged by the
        conversation's boot pass: a turn takes its wakes, ``during`` makes
        its calls, and the turn ends with ``status`` (stopped by
        ``stopped_by``)."""
        turn = f"turn_{uuid.uuid4().hex[:8]}"

        def running(w) -> None:
            w.execute("INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES "
                      "(?, ?, 1, 'wake', 'running', ?)", (turn, self.th, w.now))
            w.execute("UPDATE wakes SET state = 'taken', turn_id = ? WHERE session_id = ? AND state = 'queued'",
                      (turn, self.th))
        await self.kit.engine.write(running)
        if during is not None:
            await during()

        def finished(w) -> None:
            w.execute("UPDATE turns SET status = ? WHERE id = ?", (status, turn))
            w.execute("UPDATE wakes SET state = 'consumed' WHERE turn_id = ?", (turn,))
            ids = {"session_id": self.th, "chat_id": self.chat["id"], "thread_ts": self.root["id"], "turn_id": turn}
            w.append("turn.finished", {"status": status, **({"error": "it crashed"} if status == "failed" else {})},
                     **ids)
            if stopped_by:
                w.append("turn.stopped", {"by": stopped_by, "last_step": None}, **ids)
        ended = self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND session_id = ?", (self.th,))[0][0]
        await self.chats.stop()
        await self.kit.engine.write(finished)
        await storm_chats.shutdown()
        self.chats = storm_chats.boot(self.kit.engine, self.queue)
        await self.kit.until(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND "
                                               "session_id = ?", (self.th,))[0][0] == ended + 1)

    async def test_a_thread_input_that_replied_wakes_no_coordinator(self) -> None:
        before = self.coordinator_wakes()
        await self.input(during=lambda: self.call(self.th_token, "reply", text="Fixed: it posts to /login."))
        self.assertEqual([json.loads(c)["state"] for (c,) in self.rows("SELECT card FROM cards")], ["done"])
        self.assertEqual(self.rows("SELECT count(*) FROM hops WHERE kind = 'status'"), [(0,)])
        self.assertEqual(self.coordinator_wakes(), before)
        self.assertEqual(self.rows("SELECT state FROM hops WHERE kind = 'brief'"), [("answered",)])

    async def test_a_turn_cut_after_no_reply_needed_ends_its_input_as_finished(self) -> None:
        """The engine cuts what the model says after its own ``no_reply_needed``
        (``turn.stopped {by: turn_ended}``): the agent ended its turn, so the
        thread reads as done, not stopped, and nothing is gated."""
        async def calls() -> None:
            await self.call(self.th_token, "reply", text="Fixed.")
            await self.call(self.th_token, "no_reply_needed", reason="done")
        await self.input(status="stopped", stopped_by="turn_ended", during=calls)
        self.assertEqual([json.loads(c)["state"] for (c,) in self.rows("SELECT card FROM cards")], ["done"])
        self.assertEqual(self.thread_rows(), [("done",)])
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'gate:%'"), [(0,)])
        await self.input(status="stopped", stopped_by="coordinator", during=lambda: self.call(
            self.th_token, "reply", text="Half way."))           # anyone else's stop: not the agent's own end
        self.assertEqual(json.loads(self.rows("SELECT card FROM cards ORDER BY seq DESC LIMIT 1")[0][0])["state"],
                         "needs_reply")

    async def test_a_failed_input_still_wakes_the_coordinator(self) -> None:
        await self.input(status="failed")
        self.assertEqual(self.rows("SELECT kind, to_session FROM hops WHERE kind = 'status'"), [("status", self.pc)])
        [(priority, text)] = self.rows("SELECT priority, envelope FROM wakes WHERE id LIKE 'status:%'")
        self.assertEqual(priority, "later")
        self.assertIn("is failed", text)

    async def test_an_item_a_thread_opens_wakes_no_one_and_still_blocks_the_thread(self) -> None:
        before = self.coordinator_wakes()
        for origin, kind in (("cli", "permission"), ("agent", "approval")):
            with self.subTest(origin):
                await self.kit.engine.write(lambda w: self.queue.open_item(w, ItemSpec(
                    kind=kind, title="Run `npm test`", origin=origin, blocking=True, session_id=self.th,
                    chat_id=self.chat["id"], thread_ts=self.root["id"])))
        self.assertEqual(self.thread_rows(), [("needs_approval",)])
        self.assertEqual(self.rows("SELECT count(*) FROM hops WHERE kind = 'ask'"), [(0,)])
        self.assertEqual(self.coordinator_wakes(), before)


class DigestTests(_Project):
    async def test_the_next_turn_starts_with_what_the_coordinator_no_longer_hears(self) -> None:
        notes = SinceLastTurn()
        meta = await self.kit.engine.meta(self.pc)
        since = self.rows("SELECT created_at FROM sessions WHERE id = ?", (self.pc,))[0][0]
        self.assertIsNone(await notes.note(self.kit.engine, meta, None, since))
        await self.call(self.th_token, "report", text="Found the bad URL.")
        await self.call(self.th_token, "report", text="Fixed it locally.")       # both reach the coordinator
        asked = await self.call(self.th_token, "ask", to="founder", kind="approval", title="Deploy to staging",
                                why="to check it live")
        await self.owner_message(self.chat, "Use /api/login, not /login.", thread_ts=self.root["id"])
        await self.call(self.th_token, "submit_result", summary="Login posts to /api/login.")
        text = await notes.note(self.kit.engine, meta, None, since)
        self.assertTrue(text.startswith("<since-last-turn"))
        for said in ("“Fix login”", "Found the bad URL.", "Fixed it locally.", "Login posts to /api/login.",
                     asked["review_id"],
                     "Deploy to staging", "Use /api/login, not /login."):
            self.assertIn(said, text)
        self.kit.clock.advance(5)
        await self.kit.engine.write(lambda w: w.execute(
            "INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES "
            "('turn_p2', ?, 1, 'person', 'finished', ?)", (self.pc, w.now)))
        self.assertIsNone(await notes.note(self.kit.engine, meta, None, since))      # told once
        thread_meta = await self.kit.engine.meta(self.th)
        self.assertIsNone(await notes.note(self.kit.engine, thread_meta, None, since))


class ReplayTests(CommsTestCase):
    """One task, replayed on the fake CLI: the coordinator is woken by the
    owner's messages only, and its next turn reads what its thread did."""

    async def test_a_threads_work_wakes_its_coordinator_by_the_owners_messages_only(self) -> None:
        from tests import test_storm_chats_flows as flows
        roles = {
            flows.PC: [*flows.start("pc", bind="MID"),
                       claude.mcp_call("create_task", {"message_id": "$MID", "title": "Fix login",
                                                       "instructions": "x", **CHOICE}),
                       *flows.done("pc"), flows.next_input(), claude.mcp_call("no_reply_needed", {"reason": "ok"}),
                       *flows.done("pc", 2)],
            flows.TH: [*flows.start("th"), claude.mcp_call("report", {"text": "Found the bad URL."}),
                       claude.mcp_call("ask", {"to": "founder", "kind": "question", "title": "Keep /login too?",
                                               "why": "old links"}),
                       claude.mcp_call("submit_result", {"summary": "Login posts to /api/login."}),
                       claude.mcp_call("reply", {"text": "Fixed."}), *flows.done("th")]}
        self.kit.fake = install("claude", self.kit.root / "bin", by_prompt(
            roles, argv=[claude.version(), claude.auth_status()], probe=(claude.PROBE_FLAG, claude.initialize())))
        os.environ["CLAUDE_CLI_PATH"] = str(self.kit.fake.path)
        engine = await self.kit.boot(desk=self.queue, listeners=[self.commits], notes=[SinceLastTurn()])
        await self.queue.start(engine)
        self.chats = storm_chats.boot(engine, self.queue)
        storm_jobs.boot(self.chats)
        chat = await self.project()
        pc = await self.coordinator(chat)
        post = lambda text: self.http.post(f"/api/chat/chats/{chat['id']}/messages",
                                           json={"text": text, "client_id": uuid.uuid4().hex})
        await post("Fix the login page")
        async with self.kit.pacing():
            await self.kit.until(lambda: self.rows("SELECT state FROM tasks") == [("done",)], 20)
            await self.kit.until(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND "
                                                   "session_id <> ?", (pc,))[0][0] == 1, 20)
        self.assertEqual(self.rows("SELECT reason, count(*) FROM wakes WHERE session_id = ? GROUP BY reason", (pc,)),
                         [("message", 1)])
        await post("How is it going?")
        async with self.kit.pacing():
            await self.kit.until(lambda: self.rows("SELECT count(*) FROM events WHERE type = 'input.ended' AND "
                                                   "session_id = ?", (pc,))[0][0] == 2, 20)
        self.assertEqual(self.rows("SELECT reason, count(*) FROM wakes WHERE session_id = ? GROUP BY reason", (pc,)),
                         [("message", 2)])
        [run] = [r for r in self.kit.runs() if any("How is it going?" in json.dumps(s) for s in r["stdin"])]
        second = json.dumps(run["stdin"][-1])
        for said in ("since-last-turn", "Found the bad URL.", "Login posts to /api/login.", "Keep /login too?"):
            self.assertIn(said, second)


class WiringTests(unittest.TestCase):
    def test_the_server_starts_every_coordinator_turn_with_its_digest(self) -> None:
        """Production wiring: the engine ``server.py`` boots takes the digest
        among its turn notes."""
        tree = ast.parse((Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8"))
        [boot] = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "boot" and getattr(node.func.value, "id", None) == "storm_review"]
        [notes] = [keyword.value for keyword in boot.keywords if keyword.arg == "notes"]
        self.assertIn(SinceLastTurn.__name__, {getattr(note.func, "id", None) for note in notes.elts})
