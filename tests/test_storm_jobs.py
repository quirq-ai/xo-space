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

from services.cowork_agent.storm import jobs as storm_jobs
from services.cowork_agent.storm.comms import tools
from services.cowork_agent.storm.review.decisions import Request
from tests.fakes import claude
from tests.storm_comms_kit import CommsTestCase
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

    async def test_a_run_that_cant_open_says_why(self) -> None:
        job = self.agent_job()
        await self.chats.retire_project("demo", None)
        record = await self.fire(job["id"])
        self.assertEqual((record["status"], record["code"]), ("failed", "chat_retired"))
        self.assertIn("read-only", record["reason"])
        self.chat = await self.project("other")
        unknown = self.agent_job(name="Other", agent="no_such_agent")
        record = await self.fire(unknown["id"])
        self.assertEqual((record["status"], record["code"]), ("failed", "no_runtime"))
        self.assertEqual(self.threads(), [])

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

    async def test_an_agent_model_or_effort_is_checked_against_the_catalog(self) -> None:
        self.kit.scenario(claude.initialize())
        refused = await self.refused(self.pc_token, "create_job", **self.args(model="gpt-9"))
        self.assertIn("gpt-9 isn't one of claude_code's models: claude-haiku-4-5-20251001, default, haiku", refused)
        self.assertIn("no storm runtime", await self.refused(self.pc_token, "create_job",
                                                             **self.args(agent="no_such_agent")))
        await self.call(self.pc_token, "create_job", **self.args(model="haiku", effort="high"))

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
                                  instructions="x")
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
