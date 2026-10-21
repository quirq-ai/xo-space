"""Agent jobs (storm design ch 10 §4.5, ch 11 §7.1, §14.6, ch 12 §5.8, ch 17
§2.8; §0.44, §0.51, §0.107): the scheduler's ``agent`` kind run by storm,
the job tools, the ``create_job`` review item and its decision.

Proofs ch 18 §2 asks of PR 18: a job run opens a thread; an agent's
``create_job`` waits for the person. The engine runs on the fake claude CLI
with the memory guard holding every process back (``CommsTestCase.boot``),
so what a run writes is read from the store; the flow test at the end lets
the thread's process start and reads the input it was given.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
from fastapi import FastAPI

from routers.schedules import router as schedules_router
from services.cowork_agent.storm import jobs as storm_jobs
from services.cowork_agent.storm.comms import tools
from services.cowork_agent.storm.review.decisions import Request
from tests.fakes import claude
from tests.storm_comms_kit import CHOICE, CommsTestCase
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
        await self.boot()
        self.chat = await self.project()

    def agent_job(self, **fields) -> dict:
        payload = {"kind": "agent", "name": "Morning triage", "chat": self.chat["id"],
                   "prompt": "Triage the <new> GitHub issues.", "every_seconds": DAY,
                   "first_run_at": "2026-10-22T09:00:00+05:30", **fields}
        return scheduler.create_job(payload, now=FIRE - timedelta(hours=1))

    async def opened(self, job_id: str) -> dict:
        """The outcome of the fire being opened now."""
        return await asyncio.wrap_future(scheduler._running[job_id].future)

    async def fire(self, job_id: str, at: datetime = FIRE) -> dict:
        """One tick at ``at``, the fire opened, and the next tick's record."""
        self.assertIn(job_id, scheduler.tick(at).started)
        await self.opened(job_id)
        self.assertIn(job_id, scheduler.tick(at + timedelta(seconds=1)).finished)
        return scheduler.list_runs(job_id)[0]

    def threads(self) -> list[tuple]:
        return self.rows("SELECT ts, title, session_id FROM threads WHERE chat_id = ?", (self.chat["id"],))


class RunTests(JobsTestCase):
    async def test_a_fire_opens_a_thread_rooted_on_a_system_message_with_the_prompt_as_the_owners(self) -> None:
        job = self.agent_job()
        record = await self.fire(job["id"])
        self.assertEqual({k: record[k] for k in ("status", "trigger", "fire_at", "chat_id")},
                         {"status": "ok", "trigger": "schedule", "fire_at": FIRE_STAMP, "chat_id": self.chat["id"]})
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

    async def test_a_fire_handed_over_twice_opens_one_thread(self) -> None:
        job = self.agent_job()
        jobs = storm_jobs.current()
        both = await asyncio.gather(*(asyncio.wrap_future(jobs.fire(job, FIRE_STAMP, "schedule")) for _ in range(2)))
        self.assertEqual({o["thread_ts"] for o in both}, {self.threads()[0][0]})
        self.assertEqual(sorted(bool(o.get("already")) for o in both), [False, True])
        self.assertEqual(len(self.threads()), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM wakes WHERE id LIKE 'sched:%'"), [(1,)])

    async def test_a_duplicate_tick_a_manual_run_in_the_same_second_and_a_restart_open_one_thread(self) -> None:
        job = self.agent_job(every_seconds=None, first_run_at=None)        # manual only
        now = FIRE
        scheduler.run_now(job["id"], now=now)
        first = await self.opened(job["id"])
        scheduler.tick(now)                                                # harvests; a manual job never fires
        scheduler.run_now(job["id"], now=now)                              # the same second: the same fire
        again = await self.opened(job["id"])
        self.assertEqual((again["thread_ts"], again.get("already")), (first["thread_ts"], True))
        scheduler.tick(now)
        scheduler.run_now(job["id"], now=now + timedelta(seconds=1))
        later = await self.opened(job["id"])
        scheduler.reset_state()                                            # the server stops before the harvest
        await storm_jobs.shutdown()                                        # … and starts again
        storm_jobs.boot(self.chats)
        self.assertEqual(scheduler.tick(now + timedelta(seconds=2)).started, [job["id"]])
        handed_again = await self.opened(job["id"])
        self.assertEqual((handed_again["thread_ts"], handed_again.get("already")), (later["thread_ts"], True))
        self.assertEqual(len(self.threads()), 2)

    async def test_a_fire_reports_the_thread_it_opened_after_its_job_moved_or_its_project_went(self) -> None:
        job = self.agent_job()
        self.assertIn(job["id"], scheduler.tick(FIRE).started)
        first = await self.opened(job["id"])
        scheduler.reset_state()                                            # the server stops before the harvest
        await storm_jobs.shutdown()
        other = await self.project("other")
        scheduler.update_job(job["id"], {"kind": "agent", "name": "Morning triage", "chat": other["id"],
                                         "prompt": "Triage.", "every_seconds": DAY,
                                         "first_run_at": "2026-10-22T09:00:00+05:30"})   # the person moved it
        storm_jobs.boot(self.chats)
        self.assertIn(job["id"], scheduler.tick(FIRE + timedelta(seconds=5)).started)
        again = await self.opened(job["id"])
        self.assertEqual((again["chat_id"], again["thread_ts"], again.get("already")),
                         (self.chat["id"], first["thread_ts"], True))
        self.assertEqual(self.rows("SELECT count(*) FROM threads"), [(1,)])
        await self.chats.retire_project("demo", None)
        late = await asyncio.wrap_future(storm_jobs.current().fire(job, FIRE_STAMP, "schedule"))
        self.assertEqual((late["status"], late["thread_ts"], late.get("already")), ("ok", first["thread_ts"], True))

    async def test_a_fire_whose_wake_was_pruned_still_finds_its_thread(self) -> None:
        job = self.agent_job()
        first = await self.fire(job["id"])
        key = f"sched:{job['id']}:{FIRE_STAMP}"
        await self.kit.engine.write(lambda w: w.execute("DELETE FROM wakes WHERE id = ?", (key,)))   # retention
        again = await asyncio.wrap_future(storm_jobs.current().fire(job, FIRE_STAMP, "schedule"))
        self.assertEqual((again["thread_ts"], again.get("already")), (first["thread_ts"], True))
        self.assertEqual(self.rows("SELECT count(*) FROM threads"), [(1,)])

    async def test_a_run_that_cant_open_says_why(self) -> None:
        retired = self.chat
        await self.chats.retire_project("demo", None)
        self.chat = retired                     # a job made for the chat after it was retired, by hand
        job = self.agent_job()
        record = await self.fire(job["id"])
        self.assertEqual((record["status"], record["code"]), ("failed", "chat_retired"))
        self.assertIn("read-only", record["reason"])
        self.chat = await self.project("other")
        unknown = self.agent_job(name="Other", agent="no_such_agent")
        record = await self.fire(unknown["id"])
        self.assertEqual((record["status"], record["code"]), ("failed", "no_runtime"))
        self.assertTrue(scheduler.get_job(unknown["id"])["enabled"])      # it may run once the agent is back
        self.assertEqual(self.threads(), [])

    async def test_removing_a_project_turns_its_jobs_off_by_xo_and_keeps_their_history(self) -> None:
        job = self.agent_job()
        # Its first fire is a day later, so the first tick hands over only job's (none is left opening unheard).
        other = self.agent_job(name="Other", chat=(await self.project("other"))["id"],
                               first_run_at="2026-10-23T09:00:00+05:30")
        await self.fire(job["id"])
        await self.chats.retire_project("demo", None)
        off = scheduler.get_job(job["id"])
        self.assertFalse(off["enabled"])
        self.assertEqual((off["disabled_by"]["kind"], off["disabled_by"]["reason"]),
                         ("xo", storm_jobs.PROJECT_REMOVED))
        self.assertEqual([r["status"] for r in scheduler.list_runs(job["id"])], ["ok"])
        self.assertTrue(scheduler.get_job(other["id"])["enabled"])
        await self.project("demo")              # a new project of that name gets a new chat; the job stays off
        self.assertEqual(scheduler.tick(FIRE + timedelta(days=1)).started, [other["id"]])
        self.assertEqual((await self.opened(other["id"]))["status"], "ok")

    async def test_stopped_jobs_stop_hearing_retirements(self) -> None:
        job = self.agent_job()
        await storm_jobs.shutdown()
        await self.chats.retire_project("demo", None)
        self.assertTrue(scheduler.get_job(job["id"])["enabled"])      # its next run turns it off instead

    async def test_a_job_whose_chat_is_gone_is_turned_off_at_its_first_failed_run(self) -> None:
        for chat_id, code in ((self.chat["id"], "chat_retired"), ("chat_0000000000000000", "no_such_chat")):
            with self.subTest(code=code):
                if code == "chat_retired":
                    await self.kit.engine.write(lambda w: w.execute(       # retired behind the jobs' back
                        "UPDATE chats SET retired_at = ? WHERE id = ?", (w.now, chat_id)))
                job = self.agent_job(name=f"Gone {code[:4]}", chat=chat_id)
                record = await self.fire(job["id"])
                self.assertEqual(record["code"], code)
                off = scheduler.get_job(job["id"])
                self.assertEqual((off["enabled"], off["disabled_by"]["kind"]), (False, "xo"))
                self.assertEqual(off["disabled_by"]["reason"], record["reason"])

    async def test_a_model_the_agent_no_longer_offers_is_dropped_for_the_run_and_said(self) -> None:
        self.kit.scenario(claude.initialize())                             # the model-free models probe
        job = self.agent_job(model="gpt-9", effort="low")
        record = await self.fire(job["id"])
        ts = record["thread_ts"]
        notes = [t for (t,) in self.rows("SELECT text FROM messages WHERE thread_ts = ? AND author_kind = 'system'",
                                         (ts,))]
        self.assertTrue(any("gpt-9 isn't one of claude_code's models" in n and "default instead" in n
                            for n in notes), notes)
        self.assertEqual(self.rows("SELECT model, effort, set_by FROM intelligence WHERE scope = 'thread'"),
                         [(None, "low", "job")])


class PersonJobTests(JobsTestCase):
    """``POST /api/schedules {kind: "agent"}``: the person's own job, no review item."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        app = FastAPI()
        app.include_router(schedules_router)
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
                                     base_url="http://127.0.0.1")

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
        self.assertEqual((job["kind"], job["created_by"], job["approved_by"]), ("agent", {"kind": "person"}, None))
        self.assertEqual(self.rows("SELECT count(*) FROM review_items"), [(0,)])
        started = await self.api.post(f"/api/schedules/{job['id']}/run")
        self.assertEqual(started.status_code, 202, started.text)
        await self.opened(job["id"])
        runs = (await self.api.get(f"/api/schedules/{job['id']}/runs")).json()["runs"]
        self.assertEqual([(r["status"], r["trigger"], r["thread_ts"]) for r in runs],
                         [("ok", "manual", self.threads()[0][0])])
        edited = await self.api.put(f"/api/schedules/{job['id']}", json=self.body(prompt="Summarise the week."))
        self.assertEqual((edited.status_code, edited.json()["prompt"]), (200, "Summarise the week."))

    async def test_a_job_is_checked_before_it_is_saved(self) -> None:
        workspace = await self.chats.workspace()
        self.kit.scenario(claude.initialize())
        for body, status, code in ((self.body(chat=workspace["id"]), 400, "not_a_project_chat"),
                                   (self.body(chat="chat_nope"), 400, "no_such_chat"),
                                   (self.body(model="gpt-9"), 400, "unsupported_model"),
                                   (self.body(agent="no_such_agent"), 409, "no_runtime"),
                                   (self.body(every_seconds=0), 400, "invalid_job"),
                                   (self.body(approved_by={"review_id": "rv_forged"}), 400, "invalid_job")):
            with self.subTest(code=code):
                refused = await self.api.post("/api/schedules", json=body)
                self.assertEqual(refused.status_code, status, refused.text)
                self.assertEqual(refused.json()["detail"]["code"], code)
        self.assertEqual(scheduler.list_jobs(), [])
        self.assertEqual((await self.api.put("/api/schedules/nope", json=self.body())).status_code, 404)

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

    async def test_create_job_opens_an_item_in_plain_words_and_nothing_exists_until_allow(self) -> None:
        asked = await self.call(self.pc_token, "create_job", **self.args())
        self.assertEqual(scheduler.list_jobs(), [])
        item = await self.queue.get_item(asked["review_id"])
        self.assertEqual((item["kind"], item["risk"], item["origin"], item["chat_id"]),
                         ("create_job", "notable", "agent", self.chat["id"]))
        self.assertEqual(item["title"], "Schedule “Morning triage” in demo: every day at 09:00 UTC+05:30, "
                                        "from Thu 22 Oct 2026")
        self.assertEqual(item["detail"]["text"], "Every day at 09:00 UTC+05:30, from Thu 22 Oct 2026 in demo: "
                                                 "Triage new GitHub issues.")
        self.assertEqual([o["id"] for o in item["options"]], ["allow", "send_back"])
        decided = await self.queue.decide(asked["review_id"], _request("allow"))
        [job] = scheduler.list_jobs()
        self.assertEqual(decided["decision"]["job_id"], job["id"])
        self.assertEqual((job["chat"], job["prompt"], job["every_seconds"], job["approved_by"]),
                         (self.chat["id"], "Triage new GitHub issues.", DAY, {"review_id": asked["review_id"]}))
        self.assertEqual(job["created_by"], {"kind": "agent", "session_id": self.pc, "role": "project-coordinator"})
        self.assertEqual(self.rows("SELECT json_extract(data, '$.job_id') FROM events WHERE type = 'job.registered'"),
                         [(job["id"],)])
        allowed = [t for (i, _p, t) in self.wakes(self.pc) if i.startswith("review:")]
        self.assertIn("The person allowed:", allowed[0])
        record = await self.fire(job["id"])                 # its runs carry the item's id (ch 11 §14.6)
        [(text,)] = self.rows("SELECT envelope FROM wakes WHERE id LIKE 'sched:%'")
        self.assertIn(f'review="{asked["review_id"]}"', text)
        self.assertEqual(record["status"], "ok")

    async def test_send_back_registers_nothing_and_wakes_the_agent_with_the_note(self) -> None:
        asked = await self.call(self.pc_token, "create_job", **self.args())
        await self.queue.decide(asked["review_id"], _request("send_back", note="Make it weekly."))
        self.assertEqual(scheduler.list_jobs(), [])
        [text] = [t for (i, _p, t) in self.wakes(self.pc) if i.startswith("review:")]
        self.assertIn("sent this back", text)
        self.assertIn("Make it weekly.", text)
        await storm_jobs.current().recover()
        self.assertEqual(scheduler.list_jobs(), [])

    async def test_allow_registers_once_even_if_the_server_stopped_before_the_registration(self) -> None:
        asked = await self.call(self.pc_token, "create_job", **self.args())
        await storm_jobs.shutdown()                         # the decision commits; nothing registers it
        await self.queue.decide(asked["review_id"], _request("allow"))
        self.assertEqual(scheduler.list_jobs(), [])
        jobs = storm_jobs.boot(self.chats)                  # the next start registers it
        await self.kit.until(lambda: len(scheduler.list_jobs()) == 1)
        await jobs.recover()                                # and another start finds nothing to do
        [job] = scheduler.list_jobs()
        self.assertEqual(self.rows("SELECT json_extract(decision, '$.job_id') FROM review_items WHERE id = ?",
                                   (asked["review_id"],)), [(job["id"],)])

        second = await self.call(self.pc_token, "create_job", **self.args(title="Evening triage"))
        await storm_jobs.shutdown()
        await self.queue.decide(second["review_id"], _request("allow"))
        item = await self.queue.get_item(second["review_id"])
        # Registered with the scheduler, then the server stopped before the item recorded it.
        made = scheduler.create_job(item["detail"]["job"], approved_by={"review_id": second["review_id"]})
        await storm_jobs.boot(self.chats).recover()
        self.assertEqual(sorted(j["id"] for j in scheduler.list_jobs()), sorted([job["id"], made["id"]]))
        self.assertEqual(self.rows("SELECT count(*) FROM events WHERE type = 'job.registered'"), [(2,)])

    async def test_an_allowed_job_whose_project_was_removed_is_recorded_not_retried(self) -> None:
        asked = await self.call(self.pc_token, "create_job", **self.args())
        await storm_jobs.shutdown()
        await self.queue.decide(asked["review_id"], _request("allow"))
        await self.chats.retire_project("demo", None)
        await storm_jobs.boot(self.chats).recover()
        self.assertEqual(scheduler.list_jobs(), [])
        [(error,)] = self.rows("SELECT json_extract(decision, '$.job_error') FROM review_items WHERE id = ?",
                               (asked["review_id"],))
        self.assertIn("read-only", error)

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
        from tests.storm_engine_kit import AGENT
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
        """The item shows the job, the person allows it and the scheduler
        registers it in the same words: redaction would change a secret in
        them, so create_job refuses one instead (R1)."""
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
        job = self.agent_job()
        command = scheduler.create_job({"name": "c", "command": {"argv": ["true"], "timeout": 5}})
        listed = await self.call(self.pc_token, "list_jobs")
        self.assertEqual([(j["job_id"], j["project"], j["schedule"], j["enabled"]) for j in listed["jobs"]],
                         [(job["id"], "demo", "Every day at 09:00 UTC+05:30, from Thu 22 Oct 2026", True)])
        self.assertIn("no job", await self.refused(self.pc_token, "cancel_job", job_id=command["id"], reason="x"))
        cancelled = await self.call(self.pc_token, "cancel_job", job_id=job["id"], reason="The owner stopped it.")
        self.assertEqual(cancelled, {"ok": True, "job_id": job["id"], "enabled": False})
        off = scheduler.get_job(job["id"])
        self.assertFalse(off["enabled"])
        self.assertEqual({k: off["disabled_by"][k] for k in ("kind", "session_id", "role", "reason")},
                         {"kind": "agent", "session_id": self.pc, "role": "project-coordinator",
                          "reason": "The owner stopped it."})
        self.assertEqual(scheduler.tick(FIRE).started, [])
        self.assertEqual(self.rows("SELECT json_extract(data, '$.job_id') FROM events WHERE type = 'comms.call' AND "
                                   "json_extract(data, '$.tool') = 'cancel_job' AND json_extract(data, '$.ok')"),
                         [(job["id"],)])
        self.assertEqual([j["enabled"] for j in (await self.call(self.ws_token, "list_jobs"))["jobs"]], [False])

    async def test_a_project_only_reaches_its_own_jobs(self) -> None:
        own = self.agent_job()
        self.chat = await self.project("other")
        theirs = self.agent_job(name="Other triage")
        self.assertEqual([j["job_id"] for j in (await self.call(self.pc_token, "list_jobs"))["jobs"]], [own["id"]])
        self.assertIn("no job", await self.refused(self.pc_token, "cancel_job", job_id=theirs["id"], reason="x"))
        self.assertEqual(sorted(j["job_id"] for j in (await self.call(self.ws_token, "list_jobs"))["jobs"]),
                         sorted([own["id"], theirs["id"]]))
        self.assertEqual([j["job_id"] for j in (await self.call(self.ws_token, "list_jobs", project="other"))["jobs"]],
                         [theirs["id"]])
        self.assertTrue(scheduler.get_job(theirs["id"])["enabled"])

    async def test_a_threads_create_job_tells_its_coordinator(self) -> None:
        root = await self.owner_message(self.chat, "Triage issues every morning")
        started = await self.call(self.pc_token, "start_thread_session", message_id=root["id"], title="Triage",
                                  instructions="x", **CHOICE)
        asked = await self.call(await self.token(started["session_id"]), "create_job", **self.args())
        self.assertEqual(self.rows("SELECT kind, to_session, cause FROM hops WHERE kind = 'ask'"),
                         [("ask", self.pc, asked["review_id"])])
        self.assertEqual(self.rows("SELECT state FROM threads WHERE ts = ?", (root["id"],)), [("working",)])


class FlowTests(JobsTestCase):
    """The thread's process starts and is handed the schedule wake."""

    async def asyncSetUp(self) -> None:
        await super(JobsTestCase, self).asyncSetUp()
        await self.boot_roles({TH: [*claude.initialize(), {"expect": {"type": "user"}}, claude.init("th"),
                                    claude.mcp_call("reply", {"text": "Triaged: none new."}),
                                    *claude.text_turn("th", "ok", message_id="msg_th_1"), claude.idle("th")]})
        self.chat = await self.project()

    async def test_the_runs_process_gets_the_prompt_as_its_first_input_and_replies_in_the_thread(self) -> None:
        job = self.agent_job()
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
