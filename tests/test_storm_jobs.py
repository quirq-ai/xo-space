"""Agent jobs (storm design ch 10 §4.5, ch 11 §7.1, §14.6, ch 12 §5.8, ch 17
§2.8; §0.44, §0.51, §0.107; w5-w7-design W6): each one a timer that storm's
timer loop fires, the job tools, the ``create_job`` review item and its
decision, and the move of the scheduler's agent jobs into timers.

Proofs ch 18 §2 asks of PR 18: a job run opens a thread; an agent's
``create_job`` waits for the person. The engine runs on the fake claude CLI
with the memory guard holding every process back (``CommsTestCase.boot``),
on a clock set to the hour before the jobs' first fire, so what a run
writes is read from the store; the flow tests at the end let the thread's
process start and read the input it was given.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
from fastapi import FastAPI

from routers.schedules import router as schedules_router
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import jobs as storm_jobs
from services.cowork_agent.storm.chats import views
from services.cowork_agent.storm.comms import tools
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.records import FOUNDER, ledger, timers
from services.cowork_agent.storm.redact import REDACTED
from services.cowork_agent.storm.review.decisions import Request
from tests.fakes import claude
from tests.storm_comms_kit import CHOICE, CommsTestCase
from tests.storm_engine_kit import AGENT, message
from utils.commands import scheduler

#: The first fire: 09:00 at +05:30.
FIRE = datetime(2026, 10, 22, 3, 30, 0, tzinfo=timezone.utc)
FIRE_STAMP = "2026-10-22T03:30:00Z"
DAY = 86400
TH = "You are a thread"


def _request(decision: str, **fields) -> Request:
    return Request(decision=decision, idempotency_key=f"k-{decision}", **fields)


class JobsTestCase(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.start_at(FIRE - timedelta(hours=1))
        await self.boot()
        await storm_jobs.current().running()
        self.chat = await self.project()

    async def start_at(self, when: datetime) -> None:
        """The engine's clock reads ``when`` (set before it boots)."""
        self.kit.clock.base = when.timestamp() - self.kit.clock.offset

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.kit.clock.time(), timezone.utc).replace(microsecond=0)

    async def agent_job(self, **fields) -> dict:
        """A job as the person's route writes it, without the checks it
        makes first (so a model the agent no longer offers can be one)."""
        definition = scheduler.validate_definition({
            "kind": "agent", "name": "Morning triage", "chat": self.chat["id"],
            "prompt": "Triage the <new> GitHub issues.", "every_seconds": DAY,
            "first_run_at": "2026-10-22T09:00:00+05:30", **fields})
        made = await self.kit.engine.write(lambda w: storm_jobs.arm_job(
            w, definition, now=self.now(), asked_by=FOUNDER, approved_by=FOUNDER, by=FOUNDER,
            request_key=uuid.uuid4().hex))
        return storm_jobs.job_view(made)

    def timer(self, job_id: str) -> timers.Timer:
        conn = sqlite3.connect(self.kit.state / "engine" / "xo.db")
        try:
            return timers.get(conn, job_id)
        finally:
            conn.close()

    async def fire(self, job_id: str, at: datetime = FIRE) -> dict:
        """The clock at ``at``: the job's fire for that slot opened, and its
        record (the timer's last fire)."""
        self.kit.clock.advance(max(0.0, at.timestamp() - self.kit.clock.time()))
        storm_jobs.current().timers.kick()
        await self.kit.until(lambda: (self.timer(job_id).last or {}).get("at") == iso(at.timestamp()))
        return self.timer(job_id).last

    async def run_fire(self, job_id: str, at: str = FIRE_STAMP) -> dict:
        """The fire handed over again, as a restart before its timer
        recorded it would (the loop records nothing here)."""
        return await storm_jobs.current().run_fire(self.timer(job_id), at)

    async def move(self, job_id: str, chat: dict, **fields) -> dict:
        """The person moves the job to another project (``PUT``)."""
        return await storm_jobs.current().save({
            "kind": "agent", "name": "Morning triage", "chat": chat["id"], "prompt": "Triage.",
            "every_seconds": DAY, "first_run_at": "2026-10-22T09:00:00+05:30", **fields}, job_id)

    def threads(self) -> list[tuple]:
        return self.rows("SELECT ts, title, session_id FROM threads WHERE chat_id = ?", (self.chat["id"],))

    async def finish(self, ts: str) -> None:
        """The run thread ``ts`` did its work and is done (its process is held back here, so written as such)."""
        def done(w) -> None:
            w.execute("UPDATE wakes SET state = 'consumed', done_at = ? WHERE session_id = (SELECT session_id FROM "
                      "threads WHERE ts = ?)", (w.now, ts))
            chat_id = w.execute("SELECT chat_id FROM threads WHERE ts = ?", (ts,)).fetchone()[0]
            seq = w.append("card", {"ts": ts, "card": {"state": "done"}}, chat_id=chat_id, thread_ts=ts)
            w.execute("INSERT INTO cards (chat_id, thread_ts, seq, card, created_at) VALUES (?, ?, ?, ?, ?)",
                      (chat_id, ts, seq, '{"state":"done"}', w.now))
        await self.kit.engine.write(done)


class RunTests(JobsTestCase):
    async def test_a_fire_opens_a_thread_rooted_on_a_system_message_with_the_prompt_as_the_owners(self) -> None:
        job = await self.agent_job()
        record = await self.fire(job["id"])
        self.assertEqual({k: record[k] for k in ("status", "at", "chat_id")},
                         {"status": "ok", "at": FIRE_STAMP, "chat_id": self.chat["id"]})
        ts = record["thread_ts"]
        self.assertEqual(self.rows("SELECT author_kind, author_id, text, thread_ts, json_extract(extra, '$.job.id') "
                                   "FROM messages WHERE id = ?", (ts,)),
                         [("system", "xo", "Morning triage · 09:00", None, job["id"])])
        [(_ts, title, session)] = self.threads()
        self.assertEqual((_ts, title), (ts, "Morning triage"))
        self.assertEqual(self.rows("SELECT preset, role, chat_id, thread_ts FROM sessions WHERE id = ?", (session,)),
                         [("job", "thread", self.chat["id"], ts)])
        [(wake_id, priority, reason, text)] = self.rows(
            "SELECT id, priority, reason, envelope FROM wakes WHERE session_id = ?", (session,))
        self.assertEqual((wake_id, priority, reason), (f"sched:{job['id']}:{FIRE_STAMP}", "later", "schedule"))
        self.assertIn('<wake reason="schedule"', text)
        self.assertIn(f'<thread ts="{ts}">', text)
        self.assertIn('from="human" trust="principal" source="schedule"', text)
        self.assertIn(">Triage the &lt;new&gt; GitHub issues.</message>", text)    # copied from the job, escaped
        self.assertNotIn("review=", text)                                            # the person's own job
        self.assertEqual(self.timer(job["id"]).due_at, iso(FIRE.timestamp() + DAY))

    async def test_a_fire_handed_over_twice_opens_one_thread(self) -> None:
        job = await self.agent_job()
        both = await asyncio.gather(*(self.run_fire(job["id"]) for _ in range(2)))
        self.assertEqual({o["thread_ts"] for o in both}, {self.threads()[0][0]})
        self.assertEqual(sorted(bool(o.get("already")) for o in both), [False, True])
        self.assertEqual(len(self.threads()), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'sched:%'"), [(1,)])

    async def test_a_fire_opened_before_a_restart_and_not_recorded_opens_no_second_thread(self) -> None:
        job = await self.agent_job()
        first = await self.run_fire(job["id"])                  # opened; the server stopped before recording it
        record = await self.fire(job["id"])                     # the loop hands it over again
        self.assertEqual((record["status"], record["thread_ts"], record.get("already")),
                         ("ok", first["thread_ts"], True))
        self.assertEqual(len(self.threads()), 1)

    async def test_a_fire_reports_the_thread_it_opened_after_its_job_moved_or_its_project_went(self) -> None:
        job = await self.agent_job()
        first = await self.run_fire(job["id"])
        other = await self.project("other")
        await self.move(job["id"], other)                       # the person moved it
        again = await self.run_fire(job["id"])
        self.assertEqual((again["chat_id"], again["thread_ts"], again.get("already")),
                         (self.chat["id"], first["thread_ts"], True))
        self.assertEqual(self.rows("SELECT count(*) FROM threads"), [(1,)])
        await self.chats.retire_project("demo", None)
        late = await self.run_fire(job["id"])
        self.assertEqual((late["status"], late["thread_ts"], late.get("already")), ("ok", first["thread_ts"], True))

    async def test_a_fire_whose_wake_was_pruned_still_finds_its_thread(self) -> None:
        job = await self.agent_job()
        first = await self.fire(job["id"])
        key = f"sched:{job['id']}:{FIRE_STAMP}"
        await self.kit.engine.write(lambda w: w.execute("DELETE FROM wakes WHERE id = ?", (key,)))   # retention
        again = await self.run_fire(job["id"])
        self.assertEqual((again["thread_ts"], again.get("already")), (first["thread_ts"], True))
        self.assertEqual(self.rows("SELECT count(*) FROM threads"), [(1,)])

    async def test_a_run_that_cant_open_says_why(self) -> None:
        unknown = await self.agent_job(name="Other", agent="no_such_agent")
        record = await self.fire(unknown["id"])
        self.assertEqual((record["status"], record["code"]), ("failed", "no_runtime"))
        self.assertEqual(self.timer(unknown["id"]).state, "armed")      # it may run once the agent is back
        self.assertEqual(self.threads(), [])

    async def test_removing_a_project_cancels_its_timers_by_xo_and_keeps_their_history(self) -> None:
        job = await self.agent_job()
        other = await self.agent_job(name="Other", chat=(await self.project("other"))["id"],
                                     first_run_at="2026-10-23T09:00:00+05:30")
        await self.fire(job["id"])
        await self.chats.retire_project("demo", None)
        off = self.timer(job["id"])
        self.assertEqual((off.state, off.state_reason, off.last["status"]),
                         ("cancelled", storm_jobs.PROJECT_REMOVED, "ok"))
        self.assertEqual(self.timer(other["id"]).state, "armed")
        await self.project("demo")              # a new project of that name gets a new chat; the job stays off
        self.assertEqual((await self.fire(other["id"], FIRE + timedelta(days=1)))["status"], "ok")
        self.assertEqual(self.timer(job["id"]).last["at"], FIRE_STAMP)

    async def test_stopped_jobs_stop_hearing_retirements_and_the_next_run_turns_the_job_off(self) -> None:
        job = await self.agent_job()
        await storm_jobs.shutdown()
        await self.chats.retire_project("demo", None)
        self.assertEqual(self.timer(job["id"]).state, "armed")
        storm_jobs.boot(self.chats)
        record = await self.fire(job["id"])
        self.assertEqual(record["code"], "chat_retired")
        off = self.timer(job["id"])
        self.assertEqual((off.state, off.state_reason), ("cancelled", record["reason"]))

    async def test_a_job_whose_project_folder_is_gone_is_turned_off_at_its_first_failed_run(self) -> None:
        # The go/no-go review: a project removed while storm was down keeps its chat unretired, and its
        # job failed every fire without ever being turned off.
        job = await self.agent_job()
        shutil.rmtree(self.kit.projects / "demo")
        record = await self.fire(job["id"])
        self.assertEqual((record["status"], record["code"]), ("failed", "project_missing"))
        off = self.timer(job["id"])
        self.assertEqual((off.state, off.state_reason), ("cancelled", record["reason"]))

    async def test_a_project_removed_while_its_fire_opens_gets_no_live_thread(self) -> None:
        # The go/no-go review: the fire checked its chat, then awaited (the folder, the catalog, the worktree,
        # the runtime) while the person removed the project, and opened a live thread in the retired chat.
        job = await self.agent_job()
        real = self.chats.choose_runtime

        async def removed_meanwhile(chat, agent, purpose):
            await self.chats.retire_project("demo", None)
            return await real(chat, agent, purpose)
        with mock.patch.object(self.chats, "choose_runtime", removed_meanwhile):
            out = await self.run_fire(job["id"])
        self.assertEqual((out["status"], out.get("code")), ("failed", "chat_retired"))
        self.assertEqual(self.threads(), [])
        self.assertEqual(self.rows("SELECT count(*) FROM sessions WHERE chat_id = ? AND retired_at IS NULL",
                                   (self.chat["id"],)), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'sched:%'"), [(0,)])
        self.assertEqual(self.timer(job["id"]).state, "cancelled")

    async def test_a_fire_while_the_previous_run_is_still_working_is_skipped(self) -> None:
        # §0.117: an hourly job whose run was blocked stacked a thread, a worktree and a wake per fire.
        job = await self.agent_job(every_seconds=3600)
        first = await self.fire(job["id"])
        skipped = await self.fire(job["id"], FIRE + timedelta(hours=1))
        self.assertEqual({k: skipped.get(k) for k in ("status", "reason", "code", "thread_ts")},
                         {"status": "skipped", "reason": "the previous run is still working", "code": None,
                          "thread_ts": None})
        self.assertEqual(self.timer(job["id"]).due_at, "2026-10-22T05:30:00Z")      # the next slot
        self.assertEqual([ts for ts, _t, _s in self.threads()], [first["thread_ts"]])
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'sched:%'"), [(1,)])
        # Resolved by the person while its input still waits to run: its session is busy, so still working.
        await views.update_thread(self.chats, self.chat["id"], first["thread_ts"], resolved=True)
        again = await self.fire(job["id"], FIRE + timedelta(hours=2))
        self.assertEqual((again["status"], again["reason"]), ("skipped", "the previous run is still working"))
        self.assertEqual(len(self.threads()), 1)

    async def test_a_previous_run_taking_work_while_the_fire_opens_skips_it(self) -> None:
        job = await self.agent_job()
        first = await self.fire(job["id"])
        await self.finish(first["thread_ts"])
        [(_ts, _title, session)] = self.threads()
        real = self.chats.choose_runtime

        async def written_to_meanwhile(chat, agent, purpose):   # the person writes to the finished run
            await self.kit.engine.deliver(session, message(1))
            return await real(chat, agent, purpose)
        with mock.patch.object(self.chats, "choose_runtime", written_to_meanwhile):
            record = await self.fire(job["id"], FIRE + timedelta(days=1))
        self.assertEqual(record["status"], "skipped")
        self.assertEqual(len(self.threads()), 1)

    async def test_a_fire_after_the_previous_runs_project_was_removed_is_not_skipped(self) -> None:
        job = await self.agent_job()
        await self.fire(job["id"])                            # its thread works on
        other = await self.project("other")
        await self.chats.retire_project("demo", None)         # its session retired with the chat: it runs nothing
        self.chat = other
        self.kit.clock.advance(60)
        moved = await self.move(job["id"], other)             # the person moves it, turned on again
        self.assertEqual(moved["next_run"], iso(FIRE.timestamp() + DAY))
        record = await self.fire(job["id"], FIRE + timedelta(days=1))
        self.assertEqual(record["status"], "ok")
        self.assertEqual([ts for ts, _t, _s in self.threads()], [record["thread_ts"]])

    async def test_a_model_the_agent_no_longer_offers_is_dropped_for_the_run_and_said(self) -> None:
        self.kit.scenario(claude.initialize())                             # the model-free models probe
        job = await self.agent_job(model="gpt-9", effort="low")
        record = await self.fire(job["id"])
        ts = record["thread_ts"]
        notes = [t for (t,) in self.rows("SELECT text FROM messages WHERE thread_ts = ? AND author_kind = 'system'",
                                         (ts,))]
        self.assertTrue(any("gpt-9 isn't one of claude_code's models" in n and "default instead" in n
                            for n in notes), notes)
        self.assertEqual(self.rows("SELECT model, effort, set_by FROM intelligence WHERE scope = 'thread'"),
                         [(None, "low", "job")])

    async def test_a_fire_whose_agents_models_cant_be_read_runs_on_the_default_and_says_why(self) -> None:
        # The go/no-go review: a passing failure to read the catalog failed the fire, and a daily job lost its day.
        job = await self.agent_job(model="haiku", effort="low")
        runtime = self.kit.engine.runtime(AGENT)
        unreadable = c.RuntimeFailure("timeout", "the models probe timed out", retryable=True)
        with mock.patch.object(runtime, "models", side_effect=unreadable):
            record = await self.fire(job["id"])
        self.assertEqual(record["status"], "ok")
        notes = [t for (t,) in self.rows("SELECT text FROM messages WHERE thread_ts = ? AND author_kind = 'system'",
                                         (record["thread_ts"],))]
        self.assertIn("The job's model and effort couldn't be checked, so this run uses the thread's default: "
                      "claude_code's models can't be read now (the models probe timed out); try again shortly.", notes)
        self.assertEqual(self.rows("SELECT count(*) FROM intelligence WHERE scope = 'thread'"), [(0,)])
        definition = self.timer(job["id"]).definition
        self.assertEqual((definition["model"], definition["effort"]), ("haiku", "low"))   # kept for the next run


class PersonJobTests(JobsTestCase):
    """``POST /api/schedules {kind: "agent"}``: the person's own job, no review item."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        app = FastAPI()
        app.include_router(schedules_router)
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
                                     base_url="http://127.0.0.1")
        from tests.founder_auth_kit import connect
        connect(self.api)

    async def asyncTearDown(self) -> None:
        await self.api.aclose()
        await super().asyncTearDown()

    def body(self, **fields) -> dict:
        return {"kind": "agent", "name": "Nightly notes", "chat": self.chat["id"], "prompt": "Summarise the day.",
                "every_seconds": DAY, "first_run_at": "2026-10-22T21:00:00Z", **fields}

    async def test_the_person_creates_an_agent_job_and_its_runs_link_their_threads(self) -> None:
        made = await self.api.post("/api/schedules", json=self.body())
        self.assertEqual(made.status_code, 201, made.text)
        job = made.json()
        self.assertEqual((job["kind"], job["created_by"], job["approved_by"], job["next_run"]),
                         ("agent", {"kind": "person"}, None, "2026-10-22T21:00:00Z"))
        self.assertEqual(self.rows("SELECT count(*) FROM review_items"), [(0,)])
        self.assertEqual(scheduler.list_jobs(), [])                      # a timer, never the scheduler's
        self.assertEqual(self.rows("SELECT asked_by, approved_by, state FROM timers WHERE id = ?", (job["id"],)),
                         [(FOUNDER, FOUNDER, "armed")])
        record = await self.fire(job["id"], datetime(2026, 10, 22, 21, tzinfo=timezone.utc))
        self.assertEqual((record["status"], record["thread_ts"]), ("ok", self.threads()[0][0]))
        edited = await self.api.put(f"/api/schedules/{job['id']}", json=self.body(prompt="Summarise the week."))
        self.assertEqual((edited.status_code, edited.json()["prompt"], edited.json()["next_run"]),
                         (200, "Summarise the week.", "2026-10-23T21:00:00Z"))     # the schedule kept its slot
        off = await self.api.put(f"/api/schedules/{job['id']}", json=self.body(enabled=False))
        self.assertEqual((off.json()["enabled"], off.json()["state_reason"]), (False, storm_jobs.SAVED_OFF))
        self.assertEqual(await self.kit.engine.store.read(ledger.verify), [])

    async def test_a_job_is_checked_before_it_is_saved(self) -> None:
        workspace = await self.chats.workspace()
        self.kit.scenario(claude.initialize())
        self.kit.secrets_file.write_text("DEPLOY_PASS=made-up-store-secret-5522\n")
        for body, status, code in ((self.body(chat=workspace["id"]), 400, "not_a_project_chat"),
                                   (self.body(chat="chat_nope"), 400, "no_such_chat"),
                                   (self.body(model="gpt-9"), 400, "unsupported_model"),
                                   (self.body(agent="no_such_agent"), 409, "no_runtime"),
                                   (self.body(every_seconds=0), 400, "invalid_job"),
                                   (self.body(approved_by={"review_id": "rv_forged"}), 400, "invalid_job"),
                                   (self.body(prompt="Deploy with made-up-store-secret-5522."), 400,
                                    "secret_in_job")):
            with self.subTest(code=code):
                refused = await self.api.post("/api/schedules", json=body)
                self.assertEqual(refused.status_code, status, refused.text)
                self.assertEqual(refused.json()["detail"]["code"], code)
                self.assertNotIn("made-up-store-secret", refused.text)
        self.assertEqual(self.rows("SELECT count(*) FROM timers"), [(0,)])
        self.assertEqual((await self.api.put("/api/schedules/nope", json=self.body())).status_code, 404)

    async def test_a_project_removed_while_its_job_is_saved_refuses_the_job(self) -> None:
        # The go/no-go review: the chat was checked, then the job written; the removal came between.
        real = self.chats.runtime_for

        async def removed_meanwhile(agent, purpose):
            await self.chats.retire_project("demo", None)
            return await real(agent, purpose)
        with mock.patch.object(self.chats, "runtime_for", removed_meanwhile):
            made = await self.api.post("/api/schedules", json=self.body())
        self.assertEqual((made.status_code, made.json()["detail"]["code"]), (409, "chat_retired"))
        self.assertEqual(self.rows("SELECT count(*) FROM timers"), [(0,)])

    async def test_command_jobs_keep_their_shape(self) -> None:
        made = await self.api.post("/api/schedules", json={"name": "c", "command": {"argv": ["true"], "timeout": 5}})
        self.assertEqual(made.status_code, 201)
        self.assertNotIn("kind", made.json())
        refused = await self.api.post("/api/schedules", json={"name": "c"})
        self.assertEqual(refused.json(), {"detail": "command is required (a JSON object: argv, timeout, optional "
                                                    "cwd/env)"})


class ToolTests(JobsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        self.workspace = await self.chats.workspace()
        self.ws_token = await self.token(await self.coordinator(self.workspace))

    def args(self, **fields) -> dict:
        return {"title": "Morning triage", "prompt": "Triage new GitHub issues.", "every": {"seconds": DAY},
                "first_run_at": "2026-10-22T09:00:00+05:30", **fields}

    def jobs(self) -> list[tuple]:
        return self.rows("SELECT id, chat_id, note, every_s, asked_by, approved_by, state FROM timers "
                         "WHERE job IS NOT NULL")

    async def test_create_job_needs_the_owners_approval_and_nothing_exists_until_allow(self) -> None:
        asked = await self.call(self.pc_token, "create_job", **self.args())
        self.assertEqual(self.jobs(), [])
        item = await self.queue.get_item(asked["review_id"])
        self.assertEqual((item["kind"], item["risk"], item["origin"], item["chat_id"]),
                         ("create_job", "notable", "agent", self.chat["id"]))
        self.assertEqual(item["title"], "Schedule “Morning triage” in demo: every day at 09:00 UTC+05:30, "
                                        "from Thu 22 Oct 2026")
        self.assertEqual(item["detail"]["text"], "Every day at 09:00 UTC+05:30, from Thu 22 Oct 2026 in demo: "
                                                 "Triage new GitHub issues.")
        self.assertEqual([o["id"] for o in item["options"]], ["allow", "send_back"])
        decided = await self.queue.decide(asked["review_id"], _request("allow"))
        [(job_id, chat_id, note, every, asked_by, approved_by, state)] = self.jobs()
        self.assertEqual(decided["decision"]["job_id"], job_id)
        self.assertEqual((chat_id, note, every, asked_by, approved_by, state),
                         (self.chat["id"], "Triage new GitHub issues.", DAY, self.pc, asked["review_id"], "armed"))
        self.assertEqual(self.rows("SELECT json_extract(data, '$.job_id') FROM events WHERE type = 'job.registered'"),
                         [(job_id,)])
        allowed = [t for (i, _p, t) in self.wakes(self.pc) if i.startswith("review:")]
        self.assertIn("The person allowed:", allowed[0])
        record = await self.fire(job_id)                    # its runs carry the item's id (ch 11 §14.6)
        [(text,)] = self.rows("SELECT envelope FROM wakes WHERE id LIKE 'sched:%'")
        self.assertIn(f'review="{asked["review_id"]}"', text)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(await self.kit.engine.store.read(ledger.verify), [])

    async def test_send_back_registers_nothing_and_wakes_the_agent_with_the_note(self) -> None:
        asked = await self.call(self.pc_token, "create_job", **self.args())
        await self.queue.decide(asked["review_id"], _request("send_back", note="Make it weekly."))
        self.assertEqual(self.jobs(), [])
        [text] = [t for (i, _p, t) in self.wakes(self.pc) if i.startswith("review:")]
        self.assertIn("sent this back", text)
        self.assertIn("Make it weekly.", text)
        await storm_jobs.current().recover()
        self.assertEqual(self.jobs(), [])

    async def test_allow_registers_once_even_if_the_server_stopped_before_the_registration(self) -> None:
        asked = await self.call(self.pc_token, "create_job", **self.args())
        await storm_jobs.shutdown()                         # the decision commits; nothing registers it
        await self.queue.decide(asked["review_id"], _request("allow"))
        self.assertEqual(self.jobs(), [])
        jobs = storm_jobs.boot(self.chats)                  # the next start registers it
        await jobs.running()
        await jobs.recover()                                # and another start finds nothing to do
        [(job_id, *_rest)] = self.jobs()
        self.assertEqual(self.rows("SELECT json_extract(decision, '$.job_id') FROM review_items WHERE id = ?",
                                   (asked["review_id"],)), [(job_id,)])
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'job.registered'"), [(1,)])

    async def test_an_allowed_job_whose_project_was_removed_is_recorded_not_retried(self) -> None:
        asked = await self.call(self.pc_token, "create_job", **self.args())
        await storm_jobs.shutdown()
        await self.queue.decide(asked["review_id"], _request("allow"))
        await self.chats.retire_project("demo", None)
        await storm_jobs.boot(self.chats).running()
        self.assertEqual(self.jobs(), [])
        [(error,)] = self.rows("SELECT json_extract(decision, '$.job_error') FROM review_items WHERE id = ?",
                               (asked["review_id"],))
        self.assertIn("read-only", error)

    async def test_an_agent_told_allowed_hears_when_its_job_cant_be_registered(self) -> None:
        # The go/no-go review: the agent heard "allowed" and never that nothing was scheduled.
        (self.kit.projects / "billing-api").mkdir()
        await self.project("billing-api")
        ws = await self.coordinator(self.workspace)
        asked = await self.call(self.ws_token, "create_job", **self.args(project="billing-api"))
        await self.chats.retire_project("billing-api", None)          # removed before the person allows it
        await self.queue.decide(asked["review_id"], _request("allow"))
        self.assertEqual(self.jobs(), [])
        [(text,)] = self.rows("SELECT envelope FROM wakes WHERE id = ?", (f"job:{asked['review_id']}:refused",))
        self.assertIn(f"The owner allowed the job “Morning triage” (review item {asked['review_id']}), but it "
                      "couldn't be scheduled: This chat's project was removed", text)
        self.assertEqual(self.rows("SELECT session_id FROM wakes WHERE id = ?", (f"job:{asked['review_id']}:refused",)),
                         [(ws,)])
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'job.refused'"), [(1,)])

    async def test_a_project_removed_while_its_allowed_job_registers_registers_nothing(self) -> None:
        asked = await self.call(self.pc_token, "create_job", **self.args())
        real = storm_jobs.project_chat

        async def removed_after_the_check(chats, chat_id):
            chat = await real(chats, chat_id)
            await self.chats.retire_project("demo", None)
            return chat
        with mock.patch.object(storm_jobs, "project_chat", removed_after_the_check):
            await self.queue.decide(asked["review_id"], _request("allow"))
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.rows("SELECT json_extract(decision, '$.job_error') FROM review_items WHERE id = ?",
                                   (asked["review_id"],)), [(storm_jobs.PROJECT_REMOVED,)])

    async def test_cancel_job_leaves_a_job_moved_to_another_project(self) -> None:
        job = await self.agent_job()
        await self.move(job["id"], await self.project("other"))
        refused = await self.refused(self.pc_token, "cancel_job", job_id=job["id"], reason="Stop it.")
        self.assertIn("no job", refused)
        self.assertEqual(self.timer(job["id"]).state, "armed")

    async def test_create_job_never_asks_for_a_retired_chat_or_a_removed_project(self) -> None:
        # B4's API reference: create_job reached its chat without the read-only check every write makes, so
        # the person could be asked to allow a job that can only fail. The folder may outlive its chat's
        # retirement (a restore, or a removal XO recorded while the folder stayed).
        billing = self.kit.projects / "billing-api"
        billing.mkdir()
        await self.project("billing-api")
        original = self.chats.runtime_for

        async def retired_meanwhile(agent, purpose):           # between create_job's check and its write
            await self.kit.engine.write(lambda w: w.execute(
                "UPDATE chats SET retired_at = ? WHERE kind = 'project' AND project_name = 'billing-api'", (w.now,)))
            return await original(agent, purpose)
        with mock.patch.object(self.chats, "runtime_for", retired_meanwhile):
            raced = await self.refused(self.ws_token, "create_job", **self.args(project="billing-api"))
        self.assertIn("read-only", raced)
        self.assertIn("read-only", await self.refused(self.ws_token, "create_job", **self.args(project="billing-api")))
        billing.rmdir()
        self.assertIn("no project 'billing-api'", await self.refused(self.ws_token, "create_job",
                                                                     **self.args(project="billing-api")))
        self.assertEqual(self.rows("SELECT count(*) FROM review_items WHERE kind = 'create_job'"), [(0,)])

    async def test_a_job_always_names_a_project(self) -> None:
        (self.kit.projects / "billing-api").mkdir()
        refused = await self.refused(self.ws_token, "create_job", **self.args())
        self.assertIn("A job always runs in a project", refused)
        self.assertIn("billing-api, demo", refused)                      # the list of projects
        asked = await self.call(self.ws_token, "create_job", **self.args(project="billing-api"))
        item = await self.queue.get_item(asked["review_id"])
        self.assertEqual((item["chat_id"], item["detail"]["project"]), (self.workspace["id"], "billing-api"))
        self.assertIn("no project 'nope'", await self.refused(self.ws_token, "create_job",
                                                              **self.args(project="nope")))
        self.assertIn("its own project only, demo", await self.refused(self.pc_token, "create_job",
                                                                       **self.args(project="billing-api")))

    async def test_a_plain_chat_names_a_project_unless_it_runs_in_one(self) -> None:
        from services.cowork_agent.adapters.contract import PermissionPolicy, SessionSpec
        outside = await self.kit.engine.create_session(
            SessionSpec(cwd=str(self.kit.root), permissions=PermissionPolicy(mode="ask")), agent=AGENT,
            role="plain", preset="plain_chat")
        inside = await self.kit.engine.create_session(
            SessionSpec(cwd=str(self.kit.work), permissions=PermissionPolicy(mode="ask")), agent=AGENT,
            role="plain", preset="plain_chat")
        self.assertIn("name one with project", await self.refused(await self.token(outside), "create_job",
                                                                   **self.args()))
        asked = await self.call(await self.token(inside), "create_job", **self.args())
        self.assertEqual((await self.queue.get_item(asked["review_id"]))["detail"]["project"], "demo")

    async def test_calendar_rules_and_intervals_below_the_tick_are_refused(self) -> None:
        for every in ({"weekdays": [1, 2, 3, 4, 5]}, {"seconds": DAY, "weekdays_only": True}, "daily", 86400):
            with self.subTest(every=every):
                self.assertIn("Calendar rules", await self.refused(self.pc_token, "create_job",
                                                                   **self.args(every=every)))
        least = scheduler.min_every_seconds()
        self.assertEqual(tools.TOOLS["create_job"][2]["properties"]["every"]["properties"]["seconds"]["minimum"],
                         least)
        self.assertIn(f"at least {least}", await self.refused(self.pc_token, "create_job",
                                                              **self.args(every={"seconds": least - 1})))
        self.assertIn("UTC offset", await self.refused(self.pc_token, "create_job",
                                                       **self.args(first_run_at="2026-10-22T09:00:00")))
        self.assertIn("title is", await self.refused(self.pc_token, "create_job", **self.args(title="Triage (daily)")))
        self.assertIn("every is required", await self.refused(self.pc_token, "create_job",
                                                              **{k: v for k, v in self.args().items() if k != "every"}))
        once = await self.call(self.pc_token, "create_job", **self.args(every=None))
        self.assertTrue((await self.queue.get_item(once["review_id"]))["title"].endswith(
            "once, on Thu 22 Oct 2026 at 09:00 UTC+05:30"))

    async def test_a_title_or_prompt_holding_a_secret_is_refused_and_opens_no_item(self) -> None:
        """The item shows the job, the person allows it and storm registers
        it in the same words: redaction would change a secret in them, so
        create_job refuses one instead (R1)."""
        stored = "made-up-store-secret-4471"
        self.kit.secrets_file.write_text(f"DEPLOY_SECRET={stored}\n")
        shaped = "ghp_" + "Jb7" * 12
        for fields in ({"prompt": f"Deploy with {shaped} daily."}, {"prompt": f"Deploy with {stored} daily."},
                       {"title": f"Deploy {stored}"}):
            with self.subTest(fields=fields):
                refused = await self.refused(self.pc_token, "create_job", **self.args(**fields))
                self.assertIn("is stored and shown, so keep secrets out of it", refused)
                self.assertNotIn(shaped, refused)
                self.assertNotIn(stored, refused)
        self.assertEqual(self.rows("SELECT count(*) FROM review_items WHERE kind = 'create_job'"), [(0,)])
        await self.call(self.pc_token, "create_job", **self.args())

    async def test_an_agent_model_or_effort_is_checked_against_the_catalog(self) -> None:
        self.kit.scenario(claude.initialize())
        refused = await self.refused(self.pc_token, "create_job", **self.args(model="gpt-9"))
        self.assertIn("gpt-9 isn't one of claude_code's models: claude-haiku-4-5-20251001, default, haiku", refused)
        self.assertIn("no storm runtime", await self.refused(self.pc_token, "create_job",
                                                             **self.args(agent="no_such_agent")))
        asked = await self.call(self.pc_token, "create_job", **self.args(model="haiku", effort="high"))
        item = await self.queue.get_item(asked["review_id"])
        self.assertIn("in demo with model haiku, effort high: every day", item["title"])
        self.assertIn("in demo with model haiku, effort high: Triage", item["detail"]["text"])

    async def test_cancel_job_turns_a_job_off_at_once_records_who_and_list_jobs_shows_it(self) -> None:
        job = await self.agent_job()
        command = scheduler.create_job({"name": "c", "command": {"argv": ["true"], "timeout": 5}})
        listed = await self.call(self.pc_token, "list_jobs")
        self.assertEqual([(j["job_id"], j["project"], j["schedule"], j["enabled"], j["next_run_at"])
                          for j in listed["jobs"]],
                         [(job["id"], "demo", "Every day at 09:00 UTC+05:30, from Thu 22 Oct 2026", True, FIRE_STAMP)])
        self.assertIn("no job", await self.refused(self.pc_token, "cancel_job", job_id=command["id"], reason="x"))
        cancelled = await self.call(self.pc_token, "cancel_job", job_id=job["id"], reason="The owner stopped it.")
        self.assertEqual(cancelled, {"ok": True, "job_id": job["id"], "enabled": False})
        off = self.timer(job["id"])
        self.assertEqual((off.state, off.state_reason),
                         ("cancelled", f"Turned off by session {self.pc}: The owner stopped it."))
        self.assertEqual(self.rows("SELECT json_extract(data, '$.by') FROM events WHERE type = 'timer' AND "
                                   "json_extract(data, '$.action') = 'cancelled'"), [(self.pc,)])
        self.assertEqual(self.rows("SELECT json_extract(data, '$.job_id') FROM events WHERE type = 'comms.call' AND "
                                   "json_extract(data, '$.tool') = 'cancel_job' AND json_extract(data, '$.ok')"),
                         [(job["id"],)])
        self.kit.clock.advance(FIRE.timestamp() + 60 - self.kit.clock.time())       # its time passes: nothing runs
        storm_jobs.current().timers.kick()
        await asyncio.sleep(0.2)
        self.assertEqual(self.threads(), [])
        self.assertEqual([j["enabled"] for j in (await self.call(self.ws_token, "list_jobs"))["jobs"]], [False])

    async def test_cancel_jobs_reason_is_kept_redacted(self) -> None:
        secret = "made-up-store-secret-0044"
        self.kit.secrets_file.write_text(f"DEPLOY_PASS={secret}\n")
        job = await self.agent_job()
        await self.call(self.pc_token, "cancel_job", job_id=job["id"], reason=f"It printed {secret}.")
        self.assertEqual(self.timer(job["id"]).state_reason, f"Turned off by session {self.pc}: It printed {REDACTED}.")

    async def test_a_project_only_reaches_its_own_jobs(self) -> None:
        own = await self.agent_job()
        self.chat = await self.project("other")
        theirs = await self.agent_job(name="Other triage")
        self.assertEqual([j["job_id"] for j in (await self.call(self.pc_token, "list_jobs"))["jobs"]], [own["id"]])
        self.assertIn("no job", await self.refused(self.pc_token, "cancel_job", job_id=theirs["id"], reason="x"))
        self.assertEqual(sorted(j["job_id"] for j in (await self.call(self.ws_token, "list_jobs"))["jobs"]),
                         sorted([own["id"], theirs["id"]]))
        self.assertEqual([j["job_id"] for j in (await self.call(self.ws_token, "list_jobs", project="other"))["jobs"]],
                         [theirs["id"]])
        self.assertEqual(self.timer(theirs["id"]).state, "armed")

    async def test_a_threads_create_job_reaches_the_owner_and_wakes_no_coordinator(self) -> None:
        """Its coordinator reads it at its next turn (w5-w7-design W5)."""
        root = await self.owner_message(self.chat, "Triage issues every morning")
        started = await self.call(self.pc_token, "create_task", message_id=root["id"], title="Triage",
                                  instructions="x", **CHOICE)
        before = self.wakes(self.pc)
        asked = await self.call(await self.token(started["session_id"]), "create_job", **self.args())
        self.assertEqual(self.rows("SELECT kind, thread_ts, state FROM review_items WHERE id = ?",
                                   (asked["review_id"],)), [("create_job", root["id"], "open")])
        self.assertEqual(self.wakes(self.pc), before)
        self.assertEqual(self.thread_rows(ts=root["id"]), [("waiting",)])


class MoveTests(JobsTestCase):
    """The scheduler's agent jobs move to timers at start, once: nothing is
    lost, and nothing runs twice."""

    async def scheduler_jobs(self) -> dict[str, dict]:
        """The scheduler's files as a running Space left them: agent jobs in
        each state, a command job, and a fire handed over but not settled."""
        made = {}
        at = FIRE - timedelta(hours=2)

        def agent(name: str, **fields) -> dict:
            return scheduler.create_job({"kind": "agent", "name": name, "chat": self.chat["id"],
                                         "prompt": f"{name}: do the work.", **fields}, now=at)
        made["daily"] = agent("Daily", every_seconds=DAY, first_run_at="2026-10-22T09:00:00+05:30")
        made["hourly"] = agent("Hourly", every_seconds=3600, first_run_at="2026-10-21T00:30:00Z")
        made["off"] = agent("Off", every_seconds=DAY, first_run_at="2026-10-22T09:00:00+05:30")
        scheduler.disable_job(made["off"]["id"], by={"kind": "person"}, now=at)
        made["once"] = agent("Once", first_run_at="2026-10-22T05:00:00Z")
        made["by_hand"] = agent("By hand")
        made["allowed"] = scheduler.create_job(
            {"kind": "agent", "name": "Allowed", "chat": self.chat["id"], "prompt": "Allowed: do the work.",
             "every_seconds": DAY, "first_run_at": "2026-10-22T09:00:00+05:30"}, now=at,
            created_by={"kind": "agent", "session_id": "cse_0123456789abcdef", "role": "project-coordinator"},
            approved_by={"review_id": "rv_0123456789ab"})
        made["gone"] = scheduler.create_job({"kind": "agent", "name": "Gone", "chat": "chat_0000000000000000",
                                             "prompt": "Gone.", "every_seconds": DAY}, now=at)
        made["command"] = scheduler.create_job({"name": "Nightly tests", "command": {"argv": ["true"], "timeout": 5},
                                                "every_seconds": DAY}, now=at)
        # The hourly job's fire was handed over, then the server stopped before it was settled.
        state = json.loads(scheduler.state_file().read_text())
        state["jobs"][made["hourly"]["id"]].update(running_since="2026-10-22T02:30:00Z",
                                                   fire={"at": "2026-10-22T02:30:00Z", "trigger": "schedule"},
                                                   next_run="2026-10-22T03:30:00Z")
        scheduler.state_file().write_text(json.dumps(state))
        return made

    async def test_agent_jobs_move_to_timers_once_and_run_once_at_their_time(self) -> None:
        await storm_jobs.shutdown()
        made = await self.scheduler_jobs()
        command_before = scheduler.get_job(made["command"]["id"])
        storm_jobs.boot(self.chats)
        await storm_jobs.current().running()

        rows = {r[0]: r[1:] for r in self.rows("SELECT id, kind, due_at, every_s, state, asked_by, approved_by "
                                               "FROM timers")}
        ids = {name: job["id"] for name, job in made.items()}
        self.assertEqual(rows, {
            ids["daily"]: ("every", FIRE_STAMP, DAY, "armed", FOUNDER, FOUNDER),
            ids["hourly"]: ("every", FIRE_STAMP, 3600, "armed", FOUNDER, FOUNDER),
            ids["off"]: ("every", FIRE_STAMP, DAY, "cancelled", FOUNDER, FOUNDER),
            ids["once"]: ("at", "2026-10-22T05:00:00Z", None, "armed", FOUNDER, FOUNDER),
            ids["by_hand"]: ("at", None, None, "armed", FOUNDER, FOUNDER),
            ids["allowed"]: ("every", FIRE_STAMP, DAY, "armed", "cse_0123456789abcdef", "rv_0123456789ab")})
        self.assertEqual(self.timer(ids["off"]).state_reason, "Turned off in the scheduler by the person.")
        self.assertEqual(self.timer(ids["daily"]).note, "Daily: do the work.")
        # The fire in flight opened its thread, once, and is the hourly job's last fire.
        hourly = self.timer(ids["hourly"])
        self.assertEqual((hourly.last["at"], hourly.last["status"]), ("2026-10-22T02:30:00Z", "ok"))
        self.assertEqual(self.rows("SELECT id FROM wakes WHERE id LIKE 'sched:%'"),
                         [(f"sched:{ids['hourly']}:2026-10-22T02:30:00Z",)])
        # Off in the scheduler, with nothing in flight there; the command job is as it was.
        for name in ("daily", "hourly", "once", "by_hand", "allowed", "gone"):
            there = scheduler.get_job(ids[name])
            self.assertFalse(there["enabled"], name)
            self.assertEqual(there["disabled_by"]["kind"], "xo")
            self.assertIsNone(there.get("fire"))
            self.assertIsNone(there["running_since"])
        self.assertIn(f"timer {ids['daily']}", scheduler.get_job(ids["daily"])["disabled_by"]["reason"])
        self.assertEqual(scheduler.get_job(ids["gone"])["disabled_by"]["reason"],
                         "Its chat chat_0000000000000000 is gone, so it can't run.")
        self.assertEqual(scheduler.get_job(ids["off"])["disabled_by"], {"kind": "person", "at": "2026-10-22T01:30:00Z"})
        self.assertEqual(scheduler.get_job(made["command"]["id"]), command_before)
        files = (scheduler.jobs_file().read_bytes(), scheduler.state_file().read_bytes())

        # A second start moves nothing and opens nothing again.
        await storm_jobs.shutdown()
        storm_jobs.boot(self.chats)
        await storm_jobs.current().running()
        self.assertEqual(len(self.rows("SELECT id FROM timers")), 6)
        self.assertEqual((scheduler.jobs_file().read_bytes(), scheduler.state_file().read_bytes()), files)
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'sched:%'"), [(1,)])
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'timer' AND "
                                   "json_extract(data, '$.action') = 'arm'"), [(6,)])

        # Each runs at its time, once; the scheduler runs none of them.
        self.assertEqual(scheduler.tick(FIRE).started, [])
        await self.fire(ids["daily"])
        await self.fire(ids["allowed"])
        self.assertEqual((await self.fire(ids["once"], datetime(2026, 10, 22, 5, tzinfo=timezone.utc)))["status"],
                         "ok")
        self.assertEqual(self.timer(ids["once"]).state, "fired")
        storm_jobs.current().timers.kick()
        await asyncio.sleep(0.2)
        self.assertEqual(sorted(t for _ts, t, _s in self.threads()), ["Allowed", "Daily", "Hourly", "Once"])
        self.assertEqual(self.timer(ids["off"]).last, None)
        self.assertEqual(await self.kit.engine.store.read(ledger.verify), [])

    async def test_an_agent_job_holding_a_secret_moves_turned_off_and_never_runs_redacted(self) -> None:
        self.kit.secrets_file.write_text("DEPLOY_PASS=made-up-store-secret-7788\n")
        await storm_jobs.shutdown()
        job = scheduler.create_job({"kind": "agent", "name": "Deploy", "chat": self.chat["id"],
                                    "prompt": "Deploy with made-up-store-secret-7788 tonight.",
                                    "every_seconds": DAY, "first_run_at": "2026-10-22T09:00:00+05:30"},
                                   now=FIRE - timedelta(hours=2))
        storm_jobs.boot(self.chats)
        await storm_jobs.current().running()
        moved = self.timer(job["id"])
        self.assertEqual((moved.state, moved.state_reason, moved.note),
                         ("cancelled", storm_jobs.SECRET_KEPT_OUT, f"Deploy with {REDACTED} tonight."))
        self.assertIn("made-up-store-secret-7788", scheduler.get_job(job["id"])["prompt"])  # kept where it was
        self.kit.clock.advance(FIRE.timestamp() + 60 - self.kit.clock.time())
        storm_jobs.current().timers.kick()
        await asyncio.sleep(0.2)
        self.assertEqual(self.threads(), [])


class FlowTests(JobsTestCase):
    """The thread's process starts and is handed the schedule wake."""

    async def asyncSetUp(self) -> None:
        await super(JobsTestCase, self).asyncSetUp()
        await self.start_at(FIRE - timedelta(hours=1))
        await self.boot_roles({TH: [*claude.initialize(), {"expect": {"type": "user"}}, claude.init("th"),
                                    claude.mcp_call("reply", {"text": "Triaged: none new."}),
                                    *claude.text_turn("th", "ok", message_id="msg_th_1"), claude.idle("th")]})
        await storm_jobs.current().running()
        self.chat = await self.project()

    async def test_the_runs_process_gets_the_prompt_as_its_first_input_and_replies_in_the_thread(self) -> None:
        job = await self.agent_job()
        record = await self.fire(job["id"])
        ts = record["thread_ts"]
        async with self.kit.pacing():
            await self.kit.until(lambda: ("agent", "Triaged: none new.") in self.rows(
                "SELECT author_kind, text FROM messages WHERE thread_ts = ?", (ts,)), 20)
        [run] = self.kit.runs()
        first = run["stdin"][1]["message"]["content"][0]["text"]
        self.assertIn('<wake reason="schedule"', first)
        self.assertIn('source="schedule"', first)
        self.assertIn("Triage the &lt;new&gt; GitHub issues.", first)
        self.assertIn("--effort", run["argv"])
        self.assertEqual(run["argv"][run["argv"].index("--effort") + 1], "high")      # the job preset: a thread's

    async def test_the_next_fire_runs_once_the_previous_run_is_done(self) -> None:
        job = await self.agent_job()
        first = await self.fire(job["id"])
        self.assertEqual((await self.fire(job["id"], FIRE + timedelta(days=1)))["status"], "skipped")
        async with self.kit.pacing():
            await self.kit.until(lambda: self.thread_rows(ts=first["thread_ts"]) == [("done",)] and self.rows(
                "SELECT status FROM sessions WHERE id = (SELECT session_id FROM threads WHERE ts = ?)",
                (first["thread_ts"],)) == [("warm",)], 20)
        record = await self.fire(job["id"], FIRE + timedelta(days=2))
        self.assertEqual(record["status"], "ok")
        self.assertEqual(sorted(ts for ts, _t, _s in self.threads()), sorted([first["thread_ts"], record["thread_ts"]]))
