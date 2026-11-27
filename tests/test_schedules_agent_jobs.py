"""``/api/schedules`` for agent jobs (A1–A3): list and open them, run one now,
read its runs and delete it, all from storm's timers; what a run hands back
recorded on its job; a job's agent (``persona``) at each fire; the person's
default time zone.

The release answered 404 to every route but POST and PUT for an agent job,
because they read only the scheduler's files. Built on ``JobsTestCase``: the
engine on the fake claude CLI, its clock an hour before the first fire.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import httpx
from fastapi import FastAPI

from routers.schedules import router as schedules_router
from services.cowork_agent.storm import jobs as storm_jobs
from services.cowork_agent.storm.jobs import runs as run_records
from services.cowork_agent.storm.records import ledger, timers
from tests.test_storm_jobs import DAY, FIRE, FIRE_STAMP, JobsTestCase
from services.errors import ServiceError
from utils.commands import scheduler

SECOND_FIRE = "2026-10-23T03:30:00Z"


class ApiTestCase(JobsTestCase):
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
        return {"kind": "agent", "name": "Morning triage", "chat": self.chat["id"], "prompt": "Triage.",
                "every_seconds": DAY, "first_run_at": "2026-10-22T09:00:00+05:30", **fields}

    async def made(self, **fields) -> dict:
        made = await self.api.post("/api/schedules", json=self.body(**fields))
        self.assertEqual(made.status_code, 201, made.text)
        return made.json()

    def timer_events(self, job_id: str) -> list[dict]:
        return [json.loads(data) for (data,) in self.rows(
            "SELECT data FROM events WHERE type = 'timer' AND json_extract(data, '$.id') = ? ORDER BY seq", (job_id,))]


class ListAndOpenTests(ApiTestCase):
    async def test_agent_jobs_are_listed_and_opened_beside_command_jobs(self) -> None:
        job = await self.made(icon="🔍")
        command = await self.api.post("/api/schedules", json={"name": "c", "command": {"argv": ["true"],
                                                                                       "timeout": 5}})
        listed = (await self.api.get("/api/schedules")).json()["jobs"]
        self.assertEqual([j["id"] for j in listed], [command.json()["id"], job["id"]])
        opened = await self.api.get(f"/api/schedules/{job['id']}", params={"upcoming": 3})
        self.assertEqual(opened.status_code, 200, opened.text)
        view = opened.json()
        self.assertEqual((view["kind"], view["icon"], view["persona"], view["running"], view["last_result"]),
                         ("agent", "🔍", None, False, None))
        self.assertEqual(view["schedule"], {"every_seconds": DAY})
        self.assertEqual(view["upcoming"], [FIRE_STAMP, SECOND_FIRE, "2026-10-24T03:30:00Z"])
        self.assertEqual((await self.api.get(f"/api/schedules/{command.json()['id']}")).json()["name"], "c")
        self.assertEqual((await self.api.get("/api/schedules/nope")).status_code, 404)

    async def test_the_view_says_the_last_run_still_works_and_what_it_handed_back(self) -> None:
        job = await self.made()
        record = await self.fire(job["id"])
        view = await storm_jobs.view(job["id"])
        self.assertTrue(view["running"])
        self.assertEqual(view["last_result"]["thread"], {"chat_id": self.chat["id"], "ts": record["thread_ts"]})
        self.assertEqual((view["last_result"]["found"], view["last_result"]["summary"]), (None, None))
        await self.kit.engine.write(lambda w: storm_jobs.record_result(
            w, chat_id=self.chat["id"], thread_ts=record["thread_ts"], found=True, summary="Two new bugs."))
        await self.finish(record["thread_ts"])
        view = (await storm_jobs.views())[0]
        self.assertEqual((view["running"], view["last_result"]["found"], view["last_result"]["summary"],
                          view["last_result"]["status"]), (False, True, "Two new bugs.", "ok"))
        self.assertEqual(await self.kit.engine.store.read(ledger.verify), [])

    async def test_a_job_turned_off_has_no_upcoming_times(self) -> None:
        job = await self.made(enabled=False)
        self.assertEqual((await self.api.get(f"/api/schedules/{job['id']}")).json()["upcoming"], [])

    async def test_a_calendar_rule_reads_its_upcoming_times_from_the_rule(self) -> None:
        job = await self.agent_job()
        rule = {"days": "weekdays", "time": "09:00", "tz": "Asia/Kolkata"}
        await self.kit.engine.write(lambda w: w.execute(
            "UPDATE timers SET job = json_set(job, '$.schedule', json(?)) WHERE id = ?", (json.dumps(rule), job["id"])))
        fake = types.ModuleType("utils.schedule_rule")
        fake.upcoming = lambda r, after, n: [after + timedelta(days=k + 1) for k in range(n)]
        with mock.patch.dict(sys.modules, {"utils.schedule_rule": fake}):
            view = await storm_jobs.view(job["id"], 2)
        self.assertEqual((view["schedule"], view["upcoming"]), (rule, [FIRE_STAMP, SECOND_FIRE]))

    async def test_the_schedulers_copy_of_a_moved_job_is_never_shown(self) -> None:
        with mock.patch.object(scheduler, "list_jobs", return_value=[{"id": "old", "kind": "agent"}]), \
                mock.patch.object(scheduler, "get_job", return_value={"id": "old", "kind": "agent"}):
            self.assertEqual((await self.api.get("/api/schedules")).json(), {"jobs": []})
            self.assertEqual((await self.api.get("/api/schedules/old")).status_code, 404)

    async def test_command_jobs_are_served_while_storm_is_not_running(self) -> None:
        command = await self.api.post("/api/schedules", json={"name": "c", "command": {"argv": ["true"],
                                                                                       "timeout": 5}})
        with mock.patch.object(storm_jobs, "_jobs", None):
            listed = (await self.api.get("/api/schedules")).json()["jobs"]
            self.assertEqual([j["id"] for j in listed], [command.json()["id"]])
            self.assertEqual((await self.api.get(f"/api/schedules/{command.json()['id']}")).status_code, 200)

    async def test_a_persona_and_icon_are_checked_when_saved(self) -> None:
        for fields in ({"persona": "reviewer"}, {"persona": {"name": "reviewer"}}, {"icon": ""}, {"icon": 3}):
            with self.subTest(fields=fields):
                refused = await self.api.post("/api/schedules", json=self.body(**fields))
                self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (400, "invalid_job"))
        with mock.patch.object(run_records, "resolve_persona", return_value=None):
            gone = await self.api.post("/api/schedules", json=self.body(persona={"name": "rev", "level": "project"}))
        self.assertEqual((gone.status_code, gone.json()["detail"]["code"]), (400, "persona_missing"))
        with mock.patch.object(run_records, "resolve_persona", return_value=object()) as resolve:
            job = await self.made(persona={"name": "rev", "level": "project"})
        resolve.assert_called_once_with("demo", {"name": "rev", "level": "project"})
        self.assertEqual(job["persona"], {"name": "rev", "level": "project"})
        self.assertEqual(self.rows("SELECT count(*) FROM timers"), [(1,)])


class RunNowTests(ApiTestCase):
    async def test_run_now_opens_a_run_and_leaves_the_schedule_where_it_was(self) -> None:
        job = await self.made()
        ran = await self.api.post(f"/api/schedules/{job['id']}/run")
        self.assertEqual(ran.status_code, 202, ran.text)
        body = ran.json()
        [(ts, _title, _session)] = self.threads()
        self.assertEqual((body["ok"], body["chat_id"], body["ts"], body["job"]["running"]),
                         (True, self.chat["id"], ts, True))
        timer = self.timer(job["id"])
        self.assertEqual((timer.due_at, timer.last["trigger"], timer.last["thread_ts"]), (FIRE_STAMP, "manual", ts))
        same = await self.api.post(f"/api/schedules/{job['id']}/run")        # the same second: that run again
        self.assertEqual((same.status_code, same.json()["ts"]), (202, ts))
        self.kit.clock.advance(60)
        again = await self.api.post(f"/api/schedules/{job['id']}/run")       # its run still works
        self.assertEqual((again.status_code, again.json()["detail"]["code"]), (409, "job_running"))
        self.assertEqual(len(self.threads()), 1)
        last = self.timer(job["id"]).last                                   # the working run stays the last
        self.assertEqual((last["status"], last["thread_ts"]), ("ok", ts))
        self.assertEqual(await self.kit.engine.store.read(ledger.verify), [])

    async def test_run_now_runs_a_job_that_is_off_and_keeps_it_off(self) -> None:
        job = await self.made(enabled=False)
        ran = await self.api.post(f"/api/schedules/{job['id']}/run")
        self.assertEqual(ran.status_code, 202, ran.text)
        self.assertEqual((self.timer(job["id"]).state, len(self.threads())), ("cancelled", 1))

    async def test_run_now_says_why_a_run_could_not_open(self) -> None:
        job = await self.made()
        await self.chats.retire_project("demo", None)
        refused = await self.api.post(f"/api/schedules/{job['id']}/run")
        self.assertEqual(refused.status_code, 409)
        self.assertIn(refused.json()["detail"]["code"], storm_jobs.CHAT_GONE)


class DeleteTests(ApiTestCase):
    async def test_delete_removes_the_job_and_keeps_its_runs(self) -> None:
        job = await self.made()
        record = await self.fire(job["id"])
        with mock.patch.object(scheduler, "delete_job", side_effect=scheduler.UnknownJobError(job["id"])) as forget:
            deleted = await self.api.delete(f"/api/schedules/{job['id']}")
        self.assertEqual(deleted.json(), {"ok": True, "deleted": job["id"]})
        forget.assert_called_once_with(job["id"])           # a moved job's copy goes too, so it isn't moved back
        self.assertEqual(self.rows("SELECT count(*) FROM timers"), [(0,)])
        self.assertEqual(self.timer_events(job["id"])[-1]["action"], "delete")
        self.assertEqual(self.rows("SELECT row_sha256 FROM ledger WHERE record = ? ORDER BY seq DESC LIMIT 1",
                                   (f"timer:{job['id']}",)), [(ledger.REMOVED,)])
        self.assertEqual(await self.kit.engine.store.read(ledger.verify), [])
        self.assertEqual([ts for ts, _t, _s in self.threads()], [record["thread_ts"]])
        for call in (self.api.get(f"/api/schedules/{job['id']}"), self.api.delete(f"/api/schedules/{job['id']}"),
                     self.api.post(f"/api/schedules/{job['id']}/run")):
            self.assertEqual((await call).status_code, 404)

    async def test_a_fire_recorded_after_its_job_was_deleted_writes_nothing(self) -> None:
        job = await self.made()
        await storm_jobs.current().delete(job["id"])
        self.assertIsNone(await self.kit.engine.write(lambda w: timers.fire(
            w, timers.get(w, job["id"]), at=FIRE_STAMP, outcome={"status": "ok"}, next_due=None)))

    async def test_command_jobs_are_still_deleted_by_the_scheduler(self) -> None:
        command = (await self.api.post("/api/schedules", json={"name": "c", "command": {"argv": ["true"],
                                                                                        "timeout": 5}})).json()
        self.assertEqual((await self.api.delete(f"/api/schedules/{command['id']}")).status_code, 200)
        self.assertEqual(scheduler.list_jobs(), [])


class RunsTests(ApiTestCase):
    async def test_runs_list_each_fire_with_its_thread_and_what_it_handed_back(self) -> None:
        job = await self.made()
        first = await self.fire(job["id"])
        await self.kit.engine.write(lambda w: storm_jobs.record_result(
            w, chat_id=self.chat["id"], thread_ts=first["thread_ts"], found=False, summary="Nothing new."))
        skipped = await self.fire(job["id"], FIRE + timedelta(days=1))      # the first still works
        self.assertEqual(skipped["status"], "skipped")
        runs = await self.api.get(f"/api/schedules/{job['id']}/runs")
        self.assertEqual(runs.status_code, 200, runs.text)
        self.assertEqual(runs.json()["log_path"], None)
        newest, oldest = runs.json()["runs"]
        self.assertEqual({k: newest[k] for k in ("fire_at", "trigger", "status", "found", "thread")},
                         {"fire_at": SECOND_FIRE, "trigger": "schedule", "status": "skipped", "found": None,
                          "thread": None})
        self.assertEqual(newest["reason"], run_records.STILL_WORKING)
        self.assertEqual(oldest, {"fire_at": FIRE_STAMP, "trigger": "schedule", "status": "ok", "reason": None,
                                  "found": False, "summary": "Nothing new.",
                                  "thread": {"chat_id": self.chat["id"], "ts": first["thread_ts"],
                                             "state": oldest["thread"]["state"]}})
        self.assertIsNotNone(oldest["thread"]["state"])
        paged = await self.api.get(f"/api/schedules/{job['id']}/runs", params={"before": SECOND_FIRE, "limit": 5})
        self.assertEqual([r["fire_at"] for r in paged.json()["runs"]], [FIRE_STAMP])
        self.assertEqual([r["fire_at"] for r in await storm_jobs.current().runs(job["id"], 1)], [SECOND_FIRE])

    async def test_a_result_of_an_older_run_is_kept_in_its_history_not_on_the_last_fire(self) -> None:
        job = await self.made(every_seconds=3600)
        first = await self.fire(job["id"])
        await self.finish(first["thread_ts"])
        second = await self.fire(job["id"], FIRE + timedelta(hours=1))
        await self.kit.engine.write(lambda w: storm_jobs.record_result(
            w, chat_id=self.chat["id"], thread_ts=first["thread_ts"], found=True, summary="Late."))
        self.assertEqual((self.timer(job["id"]).last["thread_ts"], self.timer(job["id"]).last.get("found")),
                         (second["thread_ts"], None))
        runs = await storm_jobs.current().runs(job["id"])
        self.assertEqual([(r["thread"]["ts"], r["found"]) for r in runs],
                         [(second["thread_ts"], None), (first["thread_ts"], True)])
        self.assertEqual(await self.kit.engine.store.read(ledger.verify), [])

    async def test_a_thread_that_is_no_jobs_run_records_nothing(self) -> None:
        self.assertIsNone(await self.kit.engine.write(lambda w: storm_jobs.record_result(
            w, chat_id=self.chat["id"], thread_ts="cmsg_nope", found=True, summary="x")))

    async def test_runs_of_a_command_job_still_come_from_the_scheduler(self) -> None:
        command = (await self.api.post("/api/schedules", json={"name": "c", "command": {"argv": ["true"],
                                                                                        "timeout": 5}})).json()
        runs = (await self.api.get(f"/api/schedules/{command['id']}/runs")).json()
        self.assertEqual((runs["runs"], runs["log_path"]), ([], str(scheduler.log_file(command["id"]))))


class PersonaAtFireTests(ApiTestCase):
    async def test_a_run_starts_with_the_jobs_agent(self) -> None:
        persona = SimpleNamespace(name="rev", level="project", runtime=None, version="sha", problems=())
        with mock.patch.object(run_records, "resolve_persona", return_value=persona):
            job = await self.made(persona={"name": "rev", "level": "project"})
        real, given = self.chats.start_thread, []

        async def start_thread(*args, persona=None, **kwargs):
            given.append(persona)
            return await real(*args, **kwargs)
        with mock.patch.object(run_records, "resolve_persona", return_value=persona), \
                mock.patch.object(self.chats, "start_thread", start_thread):
            record = await self.fire(job["id"])
        self.assertEqual((record["status"], given), ("ok", [persona]))

    async def test_a_job_whose_agent_is_gone_is_turned_off_saying_so(self) -> None:
        with mock.patch.object(run_records, "resolve_persona", return_value=object()):
            job = await self.made(persona={"name": "rev", "level": "project"})
        with mock.patch.object(run_records, "resolve_persona", return_value=None):
            record = await self.fire(job["id"])
        self.assertEqual((record["status"], record["code"]), ("failed", "persona_missing"))
        timer = self.timer(job["id"])
        self.assertEqual((timer.state, timer.state_reason),
                         ("cancelled", run_records.PERSONA_GONE.format(name="rev", level="project")))
        self.assertEqual(self.threads(), [])


class TimeZoneTests(ApiTestCase):
    async def test_the_default_time_zone_is_read_and_set(self) -> None:
        from services.cowork_agent.storm.jobs import schedule
        saved = {"tz": "UTC"}

        def set_tz(tz: str) -> None:
            if tz == "Mars/Olympus":                            # what set_default_tz raises for a bad zone
                raise ServiceError("invalid_time_zone", "Mars/Olympus isn't a time zone", 422)
            saved["tz"] = tz
        with mock.patch.object(schedule, "default_tz", lambda: saved["tz"], create=True), \
                mock.patch.object(schedule, "set_default_tz", set_tz, create=True):
            self.assertEqual((await self.api.get("/api/schedules/time-zone")).json(), {"tz": "UTC"})
            put = await self.api.put("/api/schedules/time-zone", json={"tz": "Europe/London"})
            self.assertEqual(put.json(), {"tz": "Europe/London"})
            bad = await self.api.put("/api/schedules/time-zone", json={"tz": "Mars/Olympus"})
            self.assertEqual(bad.status_code, 422)
            for body in ({"zone": "x"}, ["x"]):
                self.assertEqual((await self.api.put("/api/schedules/time-zone", json=body)).status_code, 400)
            self.assertEqual(saved, {"tz": "Europe/London"})
